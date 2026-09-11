"""Small client for the local Codex daemon. Never starts a model turn.

Only explicit new/fork actions start the daemon. Read paths merely connect
when its socket already exists; approvals remain in the native Codex UI.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


class CodexRpcError(RuntimeError):
    """A request failed after connecting; other threads may still be readable."""


class CodexClient:
    def __init__(self, root: Path, binary: str = "codex", timeout: float = 10,
                 socket_path: Path | None = None):
        self.root, self.binary, self.timeout = root, binary, timeout
        self._socket_path = socket_path

    @property
    def socket_path(self) -> Path:
        return self._socket_path or self.root / "app-server-control/app-server-control.sock"

    def stop_thread_activity(self, thread_id: str) -> None:
        """Stop only this remote conversation before its TUI disconnects.

        Disconnecting a shared-server client does not interrupt its turn.
        Never stop the shared daemon: other conversations may still be using it.
        """
        params = {"threadId": thread_id, "limit": 1, "sortDirection": "desc"}
        thread = self.call("thread/read", {"threadId": thread_id}).get("thread", {})
        status = thread.get("status", {}).get("type")
        if status == "notLoaded":
            return
        # An idle, brand-new thread has no rollout yet; turns/list explicitly
        # rejects it. It also has no active turn to interrupt.
        turns = [] if status == "idle" else self.call("thread/turns/list", params).get("data", [])
        if turns and turns[0].get("status") == "inProgress":
            turn_id = turns[0]["id"]
            self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            deadline = time.monotonic() + self.timeout
            while True:
                turns = self.call("thread/turns/list", params).get("data", [])
                if not turns or turns[0].get("status") != "inProgress":
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Codex is still stopping its turn; retry restart shortly.")
                time.sleep(0.1)
        self.call("thread/backgroundTerminals/clean", {"threadId": thread_id})

    def call(self, method: str, params: dict) -> dict:
        from websockets.sync.client import unix_connect
        from websockets.exceptions import WebSocketException

        try:
            with unix_connect(str(self.socket_path), uri="ws://localhost/rpc",
                              open_timeout=self.timeout, close_timeout=1,
                              max_size=16 * 1024 * 1024, compression=None) as ws:
                def request(ident, method, params):
                    ws.send(json.dumps({"id": ident, "method": method, "params": params}))
                    deadline = time.monotonic() + self.timeout
                    while True:
                        msg = json.loads(ws.recv(timeout=max(0, deadline - time.monotonic())))
                        if not isinstance(msg, dict) or msg.get("id") != ident or "method" in msg:
                            continue
                        if "error" in msg:
                            raise CodexRpcError(f"Codex {method}: {msg['error']}")
                        return msg.get("result", {})

                request(1, "initialize", {"clientInfo": {"name": "cagents", "version": "0.1.0"},
                                          "capabilities": {"experimentalApi": True}})
                ws.send(json.dumps({"method": "initialized"}))
                return request(2, method, params)
        except (OSError, WebSocketException, TimeoutError, ValueError) as error:
            raise RuntimeError(f"Codex daemon: {error}") from error

    def create(self, cwd: str, parent: str = "", model: str = "") -> str:
        env = {**os.environ, "CODEX_HOME": str(self.root)}
        proc = subprocess.run([self.binary, "app-server", "daemon", "start"],
                              env=env, capture_output=True, text=True, timeout=20)
        if proc.returncode:
            raise RuntimeError("Could not start Codex daemon: " + proc.stderr.strip())
        params = {"cwd": cwd}
        if model:
            params["model"] = model
        if parent:
            params.update(threadId=parent, excludeTurns=True)
        result = self.call("thread/fork" if parent else "thread/start", params)
        thread = result.get("thread", {})
        sid = thread.get("id")
        if not isinstance(sid, str) or not sid:
            raise RuntimeError("Codex did not return a thread ID")
        # Empty thread/start results have a reserved path but no rollout yet.
        # Even thread/resume on the same daemon rejects them, so the TUI
        # exits immediately. Let Codex persist a small integration context
        # record through its own API before we attach. This adds no user
        # message and starts no model turn. Existing fork history is untouched.
        path = thread.get("path")
        if not path or not Path(path).is_file():
            self.call("thread/inject_items", {
                "threadId": sid,
                "items": [{"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "Conversation opened in cagents."}
                ]}],
            })
        return sid


class CliCodexRunner:
    """Read-only one-shot generation for an explicitly requested handoff."""
    def __init__(self, root: Path, cwd: str, binary: str = "codex", context: str = ""):
        self.root, self.cwd, self.binary, self.context = root, cwd, binary, context

    def run(self, prompt: str) -> str:
        import tempfile
        # Read the final answer from Codex's dedicated output file, not progress
        # messages on stdout. Pass prompts on stdin so shell quoting/ARG_MAX do
        # not constrain handoffs.
        with tempfile.TemporaryDirectory(prefix="cagents-codex-") as tmp:
            output = Path(tmp) / "answer.txt"
            proc = subprocess.run(
                [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                 "-C", self.cwd, "--output-last-message", str(output), "-"],
                input=self.context + "\n\n" + prompt, text=True, capture_output=True, timeout=180,
                env={**os.environ, "CODEX_HOME": str(self.root)},
            )
            if proc.returncode:
                raise RuntimeError("codex exec failed: " + proc.stderr.strip()[-500:])
            if not output.is_file():
                raise RuntimeError("Codex returned no final answer")
            return output.read_text(encoding="utf-8")
