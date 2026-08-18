"""Talking to the user's tmux server for Claude sessions.

Claude sessions on this machine run inside tmux on a dedicated socket
(`tmux -L claude`), created by the user's `claude` wrapper. cagents uses
that same socket, so:

- attaching from cagents hands off to the *real* live CLI, and detaching
  never kills anything;
- liveness is real (a tmux session exists or it doesn't), not inferred
  from process tables.

Sessions cagents itself creates get a CAGENTS_SESSION_ID tmux environment
variable so they can be mapped back to a Claude session id exactly. For
sessions created outside cagents we fall back to matching the pane's
working directory (see sessions.py).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SOCKET = "claude"
_FIELD_SEP = "\x1f"

_LIST_FORMAT = _FIELD_SEP.join(
    [
        "#{session_name}",
        "#{session_created}",
        "#{session_activity}",
        "#{session_attached}",
        "#{pane_pid}",
        "#{pane_current_path}",
    ]
)


@dataclass
class TmuxSession:
    name: str
    created: float
    activity: float
    attached: bool
    pane_pid: int
    pane_path: str
    cagents_session_id: str = ""  # from the CAGENTS_SESSION_ID env var, if set


class TmuxClient:
    """Thin wrapper over the tmux CLI. All methods are safe to call when no
    server is running (they report an empty world rather than raising)."""

    def __init__(self, socket: str = SOCKET, tmux_bin: str = "tmux"):
        self.socket = socket
        self.tmux_bin = tmux_bin

    def _run(self, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.tmux_bin, "-L", self.socket, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def available(self) -> bool:
        return shutil.which(self.tmux_bin) is not None

    def list_sessions(self) -> list[TmuxSession]:
        try:
            proc = self._run("list-panes", "-a", "-F", _LIST_FORMAT)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []  # no server running is the common case
        sessions: dict[str, TmuxSession] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(_FIELD_SEP)
            if len(parts) != 6:
                continue
            name, created, activity, attached, pane_pid, pane_path = parts
            if name in sessions:
                continue  # first pane per session is enough
            try:
                sessions[name] = TmuxSession(
                    name=name,
                    created=float(created),
                    activity=float(activity),
                    attached=attached not in ("0", ""),
                    pane_pid=int(pane_pid),
                    pane_path=pane_path,
                )
            except ValueError:
                continue
        result = list(sessions.values())
        for sess in result:
            sess.cagents_session_id = self.get_session_env(sess.name, "CAGENTS_SESSION_ID")
        return result

    def get_session_env(self, session_name: str, var: str) -> str:
        try:
            proc = self._run("show-environment", "-t", f"={session_name}", var)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        line = proc.stdout.strip()
        if "=" in line and not line.startswith("-"):
            return line.split("=", 1)[1]
        return ""

    def capture_pane(self, session_name: str, lines: int = 40) -> str:
        """The visible tail of a session's active pane, for prompt detection."""
        try:
            # "=name:" — exact session match, default window/pane. A bare
            # "=name" resolves for attach/has-session but NOT for pane
            # targets (verified against tmux 3.6a).
            proc = self._run("capture-pane", "-p", "-t", f"={session_name}:", "-S", f"-{lines}")
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def attach(self, session_name: str) -> int:
        """Attach to a live session. Blocks until the user detaches or the
        session ends. Must be called with the terminal handed over
        (e.g. inside App.suspend())."""
        import os

        env = os.environ.copy()
        # Allow attaching from inside another tmux (different socket): tmux
        # refuses to nest only because $TMUX is set.
        env.pop("TMUX", None)
        proc = subprocess.run(
            [self.tmux_bin, "-L", self.socket, "attach-session", "-t", f"={session_name}"],
            env=env,
        )
        return proc.returncode

    def send_text(self, session_name: str, text: str, submit: bool = True) -> None:
        """Type `text` into a session's Claude prompt via bracketed paste
        (so newlines don't submit early), then press Enter.

        Raises RuntimeError on failure — sending review comments into the
        wrong void must never be silent."""
        import time

        try:
            load = subprocess.run(
                [self.tmux_bin, "-L", self.socket, "load-buffer", "-b", "cagents-send", "-"],
                input=text,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if load.returncode != 0:
                raise RuntimeError(f"tmux load-buffer failed: {load.stderr.strip()}")
            paste = self._run(
                "paste-buffer", "-p", "-d", "-b", "cagents-send", "-t", f"={session_name}:"
            )
            if paste.returncode != 0:
                raise RuntimeError(f"tmux paste-buffer failed: {paste.stderr.strip()}")
            if submit:
                time.sleep(0.3)  # let the CLI ingest the paste before Enter
                enter = self._run("send-keys", "-t", f"={session_name}:", "Enter")
                if enter.returncode != 0:
                    raise RuntimeError(f"tmux send-keys failed: {enter.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"tmux send failed: {error}")

    def new_claude_session(
        self,
        directory: str,
        claude_args: list[str],
        session_id: str = "",
        claude_bin: str = "",
    ) -> str:
        """Create a detached tmux session running the real claude CLI in
        `directory`, then return the tmux session name. The caller attaches
        separately (so failures here stay distinguishable from attach
        failures)."""
        name = self._unique_name(Path(directory).name)
        claude_bin = claude_bin or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        cmd = " ".join(_shquote(a) for a in [claude_bin, *claude_args])
        env_args: list[str] = []
        if session_id:
            env_args = ["-e", f"CAGENTS_SESSION_ID={session_id}"]
        proc = self._run(
            "new-session", "-d", *env_args, "-s", name, "-c", directory, cmd,
            timeout=10.0,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {proc.stderr.strip() or 'unknown error'}")
        if session_id:
            # -e sets it for processes; set-environment makes it queryable too.
            self._run("set-environment", "-t", f"={name}", "CAGENTS_SESSION_ID", session_id)
        return name

    def has_session(self, session_name: str) -> bool:
        try:
            proc = self._run("has-session", "-t", f"={session_name}")
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def _unique_name(self, base: str) -> str:
        # Mirror the naming scheme of the user's claude-tmux wrapper:
        # sanitize the directory basename, then suffix -2, -3... if taken.
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-") or "session"
        existing = {s.name for s in self.list_sessions()}
        name, i = base, 1
        while name in existing:
            i += 1
            name = f"{base}-{i}"
        return name


def _shquote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


# Textual patterns in a Claude CLI pane that mean "waiting on a human choice"
# (permission prompt, plan approval, AskUserQuestion, trust dialog).
_PROMPT_MARKERS = (
    "Do you want",
    "Would you like",
    "Yes, and don't ask again",
    "Waiting for your response",
    "Do you trust the files",
)

# The selection cursor on a numbered option — the signature of an actual
# dialog. Claude's *conversation text* frequently contains phrases like
# "Do you want me to…", so a phrase alone must never count as a prompt.
_CHOICE_ROW = re.compile(r"❯\s*\d+\.")

# Patterns that mean Claude is actively running a turn.
_WORKING_MARKERS = (
    "esc to interrupt",
    "ctrl+b to run in background",
)


def pane_shows_prompt(pane_text: str) -> bool:
    """True only for a real dialog: a ❯-cursored numbered choice AND a
    prompt phrase, both visible. Either alone is too easy to fake with
    ordinary conversation output."""
    if not _CHOICE_ROW.search(pane_text):
        return False
    return any(marker in pane_text for marker in _PROMPT_MARKERS)


def extract_prompt_question(pane_text: str) -> str:
    """The actual question a blocked session is asking, lifted from its pane
    (e.g. "Do you want to proceed?"). Empty string when nothing matches."""
    for line in pane_text.splitlines():
        if any(marker in line for marker in _PROMPT_MARKERS):
            cleaned = line.strip().strip("│┃▏▕|").strip()
            if cleaned.startswith("❯"):
                continue  # that's the answer row, not the question
            if cleaned:
                return cleaned[:110]
    return ""


def pane_shows_working(pane_text: str) -> bool:
    return any(marker in pane_text for marker in _WORKING_MARKERS)
