"""Drive the real dashboard through a PTY; retain pane/log evidence after QA.

Only disposable tmux servers and provider homes are used. No app methods are
stubbed: input passes through the same terminal client and shims as user input.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid


class Dashboard:
    def __init__(self, artifacts):
        self.artifacts = Path(artifacts)
        # Codex's Unix socket must fit macOS's socket-path limit.
        self.temp = tempfile.TemporaryDirectory(prefix="cgqa-", dir="/tmp")
        self.path = Path(self.temp.name)
        self.socket = "cg-dashboard-" + uuid.uuid4().hex[:10]
        self.state = self.path / "state.json"
        self.state.write_text(json.dumps({"version": 1, "sessions": {}, "settings": {
            "desktop_notifications": False, "auto_done_duration": "off"}}))
        self.env = {**os.environ, "CAGENTS_DIRECT_SOCKET": self.socket,
                    "CAGENTS_SOCKET_SUFFIX": "-" + self.socket,
                    "CAGENTS_LAUNCH_CWD": str(self.path), "TERM": "xterm-256color",
                    "COLORTERM": "truecolor", "CODEX_HOME": str(self.path / "codex"),
                    "CLAUDE_CONFIG_DIR": str(self.path / "claude")}
        for key in ("TMUX", "TMUX_PANE", "CAGENTS_SIDECAR", "CAGENTS_DIRECT",
                    "CAGENTS_DIRECT_RAIL", "NO_COLOR"):
            self.env.pop(key, None)
        self.clients = []
        self.master = None
        self.probe_number = 0

    def tmux(self, *args):
        result = subprocess.run(["tmux", "-L", self.socket, *args], env=self.env,
                                text=True, capture_output=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""

    def pane(self, pane, field):
        return self.tmux("display-message", "-p", "-t", pane, "#{" + field + "}")

    @property
    def rail(self):
        return self.tmux("show-option", "-gqv", "@cagents_rail")

    @property
    def active(self):
        return self.tmux("display-message", "-p", "#{pane_id}")

    def capture(self, pane, ansi=False):
        return self.tmux("capture-pane", "-ep" if ansi else "-p", "-t", pane)

    def agents(self):
        return sorted(row for row in self.tmux("list-panes", "-a", "-F",
                      "#{@cagents_role}:#{pane_id}:#{pane_pid}").splitlines()
                      if row.startswith("agent:"))

    def messages(self):
        return [line for line in self.tmux("show-messages").splitlines() if " message:" in line]

    def assert_no_tmux_messages(self):
        messages = self.messages()
        if messages:
            self.save_artifacts("tmux-message")
        assert not messages, "Unexpected tmux status-bar messages: " + repr(messages)

    def pump(self, duration=.15):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if select.select([self.master], [], [], .02)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                for match in re.finditer(rb"\x1b\](10|11);\?(?:\x07|\x1b\\)", data):
                    value = b"2121/2525/2b2b" if match[1] == b"11" else b"dddd/dddd/dddd"
                    os.write(self.master, b"\x1b]" + match[1] + b";rgb:" + value + b"\x1b\\")

    def send(self, data, pause=.15):
        os.write(self.master, data.encode() if isinstance(data, str) else data)
        self.pump(pause)

    def wait(self, condition, message, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            if condition():
                return
        self.save_artifacts("failure")
        raise AssertionError(message + "\n" + self.capture(self.active)
                             + "\nDashboard:\n" + self.capture(self.rail)
                             + f"\nArtifacts: {self.artifacts}")

    def launch(self):
        pid, master = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 140, 0, 0))
            os.execvpe(sys.executable, [sys.executable, "-m", "cagents.direct_cli",
                       "--store", str(self.state), "--claude-dir", str(self.path / "claude"),
                       "--codex-dir", str(self.path / "codex")], self.env)
        self.clients.append((pid, master))
        self.master = master
        self.wait(lambda: "new-term" in self.tmux("list-windows", "-F", "#W")
                  and self.pane(self.rail, "pane_dead") == "0", "dashboard failed to start")
        self.pump(.5)

    def queue(self):
        self.send(b"\x07")
        self.wait(lambda: self.active == self.rail, "Ctrl-G did not reach the queue")
        self.pump(.3)
        self.assert_no_tmux_messages()

    def check_shell_input(self):
        self.probe_number += 1
        marker = self.path / f"shell-input-{self.probe_number}"
        self.send(f"printf '%s' SHELL_READY > {shlex.quote(str(marker))}\r")
        self.wait(marker.exists, "shell did not execute typed input")
        assert marker.read_text() == "SHELL_READY"
        assert self.pane(self.active, "pane_dead") == "0"

    def new_shell(self):
        self.queue()
        before = self.agents()
        self.send(b"n")
        self.wait(lambda: len(self.agents()) == len(before) + 1, "n did not create a conversation")
        row = next(row for row in self.agents() if row not in before)
        pane = row.split(":")[1]
        self.wait(lambda: self.active == pane, "new conversation did not receive focus")
        self.check_shell_input()
        self.assert_no_tmux_messages()
        return pane

    def select_session(self, sid):
        self.queue()
        def selected():
            try:
                return json.loads((self.path / "context.json").read_text()).get("session_id")
            except (OSError, ValueError):
                return None
        for _ in range(len(self.agents()) + 3):
            if selected() == sid:
                break
            self.send(b"\x1b[B", .3)
        assert selected() == sid, "conversation was not reachable through the queue"
        self.send(b"\r")
        self.wait(lambda: self.active != self.rail and self.pane(self.active, "@cagents_session_id") == sid,
                  "Enter did not attach the selected conversation")
        self.assert_no_tmux_messages()

    def save_artifacts(self, label):
        self.artifacts.mkdir(parents=True, exist_ok=True)
        (self.artifacts / f"{label}-messages.txt").write_text("\n".join(self.messages()))
        rows = self.tmux("list-panes", "-a", "-F",
                         "#{pane_id} #{pane_pid} #{pane_dead} #{pane_current_command} #{@cagents_role} #{@cagents_session_id}")
        (self.artifacts / f"{label}-panes.txt").write_text(rows)
        for row in rows.splitlines():
            pane = row.split()[0]
            (self.artifacts / f"{label}-{pane[1:]}.ansi").write_text(self.capture(pane, ansi=True))
        for name in ("ctx.log", "context.json", "state.json"):
            if (self.path / name).exists():
                shutil.copyfile(self.path / name, self.artifacts / name)

    def close(self):
        self.save_artifacts("final")
        if (self.path / "codex/app-server-control/app-server-control.sock").exists():
            subprocess.run([shutil.which("codex"), "app-server", "daemon", "stop"],
                           env=self.env, capture_output=True, timeout=20)
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], env=self.env,
                       capture_output=True, timeout=5)
        for pid, master in self.clients:
            os.close(master)
            os.waitpid(pid, 0)
        self.temp.cleanup()
