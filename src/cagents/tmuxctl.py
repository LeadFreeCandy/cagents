"""Talking to tmux about Claude sessions — across two sockets.

Hard-won constraint (reproduced 6/6 against claude v2.1.234): starting a
claude process on a tmux socket that already hosts a live claude crashes
the new process instantly. So:

- cagents SPAWNS sessions only on its own private socket
  (`cagents-sessions`), where it controls what runs;
- it still DISCOVERS and attaches sessions on the user's `claude` socket
  (the claude-tmux wrapper's home) — attaching is just a client, safe.

Every TmuxSession knows which socket it lives on; all operations take the
socket with the name.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CREATE_SOCKET = "cagents-sessions"
DISCOVER_SOCKETS = ("claude", CREATE_SOCKET)
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
    socket: str = CREATE_SOCKET
    cagents_session_id: str = ""  # from the CAGENTS_SESSION_ID env var, if set

    @property
    def key(self) -> str:
        """Unique across sockets (names may repeat between servers)."""
        return f"{self.socket}:{self.name}"


class TmuxClient:
    """Thin wrapper over the tmux CLI. Safe to call when no server is
    running (reports an empty world rather than raising)."""

    def __init__(
        self,
        sockets: tuple[str, ...] = DISCOVER_SOCKETS,
        create_socket: str = CREATE_SOCKET,
        tmux_bin: str = "tmux",
    ):
        self.sockets = sockets
        self.create_socket = create_socket
        self.tmux_bin = tmux_bin
        self._mouse_enabled: set[str] = set()

    def _run(self, socket: str, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.tmux_bin, "-L", socket, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def available(self) -> bool:
        return shutil.which(self.tmux_bin) is not None

    def list_sessions(self) -> list[TmuxSession]:
        result: list[TmuxSession] = []
        for socket in self.sockets:
            result.extend(self._list_on(socket))
        return result

    def _list_on(self, socket: str) -> list[TmuxSession]:
        try:
            proc = self._run(socket, "list-panes", "-a", "-F", _LIST_FORMAT)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []  # no server on this socket — normal
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
                    socket=socket,
                )
            except ValueError:
                continue
        found = list(sessions.values())
        for sess in found:
            sess.cagents_session_id = self.get_session_env(
                sess.name, "CAGENTS_SESSION_ID", socket=socket
            )
        return found

    def get_session_env(self, session_name: str, var: str, socket: str | None = None) -> str:
        socket = socket or self.create_socket
        try:
            proc = self._run(socket, "show-environment", "-t", f"={session_name}", var)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        line = proc.stdout.strip()
        if "=" in line and not line.startswith("-"):
            return line.split("=", 1)[1]
        return ""

    def capture_pane(self, session_name: str, lines: int = 40, socket: str | None = None) -> str:
        """The visible tail of a session's active pane, for prompt detection."""
        socket = socket or self.create_socket
        try:
            # "=name:" — exact session match, default window/pane. A bare
            # "=name" resolves for attach/has-session but NOT for pane
            # targets (verified against tmux 3.6a).
            proc = self._run(
                socket, "capture-pane", "-p", "-t", f"={session_name}:", "-S", f"-{lines}"
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def attach(self, session_name: str, socket: str | None = None) -> int:
        """Attach to a live session (blocks; call with the terminal handed
        over, e.g. inside App.suspend())."""
        import os

        socket = socket or self.create_socket
        env = os.environ.copy()
        env.pop("TMUX", None)  # deliberate nesting is fine once TMUX is unset
        proc = subprocess.run(
            [self.tmux_bin, "-L", socket, "attach-session", "-t", f"={session_name}"],
            env=env,
        )
        return proc.returncode

    def send_text(
        self, session_name: str, text: str, submit: bool = True, socket: str | None = None
    ) -> None:
        """Type `text` into a session's Claude prompt via bracketed paste
        (so newlines don't submit early), then press Enter. Raises on
        failure — messages must never vanish silently."""
        import time

        socket = socket or self.create_socket
        try:
            load = subprocess.run(
                [self.tmux_bin, "-L", socket, "load-buffer", "-b", "cagents-send", "-"],
                input=text,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            if load.returncode != 0:
                raise RuntimeError(f"tmux load-buffer failed: {load.stderr.strip()}")
            paste = self._run(
                socket, "paste-buffer", "-p", "-d", "-b", "cagents-send",
                "-t", f"={session_name}:",
            )
            if paste.returncode != 0:
                raise RuntimeError(f"tmux paste-buffer failed: {paste.stderr.strip()}")
            if submit:
                time.sleep(0.3)  # let the CLI ingest the paste before Enter
                enter = self._run(socket, "send-keys", "-t", f"={session_name}:", "Enter")
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
        """Create a detached session running the real claude CLI — always on
        the private create socket (spawning next to a live claude on a
        shared socket crashes it; see module docstring)."""
        name = self._unique_name(Path(directory).name)
        claude_bin = claude_bin or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        cmd = " ".join(_shquote(a) for a in [claude_bin, *claude_args])
        env_args: list[str] = []
        if session_id:
            env_args = ["-e", f"CAGENTS_SESSION_ID={session_id}"]
        proc = self._run(
            self.create_socket,
            "new-session", "-d", *env_args, "-s", name, "-c", directory, cmd,
            timeout=10.0,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {proc.stderr.strip() or 'unknown error'}")
        if session_id:
            self._run(
                self.create_socket,
                "set-environment", "-t", f"={name}", "CAGENTS_SESSION_ID", session_id,
            )
        # Mouse wheel should scroll these sessions when previewed.
        if self.create_socket not in self._mouse_enabled:
            self._run(self.create_socket, "set", "-g", "mouse", "on")
            self._mouse_enabled.add(self.create_socket)
        return name

    def has_session(self, session_name: str, socket: str | None = None) -> bool:
        socket = socket or self.create_socket
        try:
            proc = self._run(socket, "has-session", "-t", f"={session_name}")
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def _unique_name(self, base: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-") or "session"
        existing = {s.name for s in self._list_on(self.create_socket)}
        name, i = base, 1
        while name in existing:
            i += 1
            name = f"{base}-{i}"
        return name

    # Session-scoped statusline shown during fullscreen attaches, so the
    # cagents keys stay visible under the Claude terminal.
    _STATUS_OPTIONS = {
        "status": "on",
        "status-position": "bottom",
        "status-style": "bg=colour235,fg=colour246",
        "status-left": " cagents ",
        "status-left-style": "bg=colour31,fg=colour231,bold",
        "status-right": " ← back · C-s shell · C-d diff ",
        "status-right-length": "50",
        "window-status-format": "",
        "window-status-current-format": "",
    }

    def session_statusline_on(self, session_name: str, socket: str | None = None) -> None:
        socket = socket or self.create_socket
        for option, value in self._STATUS_OPTIONS.items():
            self._run(socket, "set", "-t", f"={session_name}", option, value)

    def session_statusline_off(self, session_name: str, socket: str | None = None) -> None:
        socket = socket or self.create_socket
        for option in self._STATUS_OPTIONS:
            self._run(socket, "set", "-u", "-t", f"={session_name}", option)

    def bind_left_detach(self, client_tty: str, socket: str | None = None) -> None:
        """Fullscreen-mode left-arrow capture: while a cagents attach is
        active, Left detaches *our* client (returning to the list). The
        client_tty filter keeps every other client's Left untouched."""
        socket = socket or self.create_socket
        proc = self._run(socket, *left_detach_bind_args(client_tty))
        if proc.returncode != 0:
            raise RuntimeError(f"tmux bind failed: {proc.stderr.strip()}")

    def unbind_left_detach(self, socket: str | None = None) -> None:
        self._run(socket or self.create_socket, "unbind", "-n", "Left")


def _shquote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def left_detach_bind_args(client_tty: str) -> list[str]:
    """Root-table binding: Left detaches the client on `client_tty`; every
    other client gets its Left passed straight through."""
    return [
        "bind", "-n", "Left",
        "if", "-F", "#{==:#{client_tty}," + client_tty + "}",
        "detach-client", "send-keys Left",
    ]


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


def pane_shows_working(pane_text: str) -> bool:
    return any(marker in pane_text for marker in _WORKING_MARKERS)


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
