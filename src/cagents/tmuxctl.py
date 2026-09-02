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

from .sockets import socket_name

CREATE_SOCKET = socket_name("cagents-sessions")
# the wrapper's own socket, suffixed too: an isolated instance must not
# pick up the real one's sessions (see sockets.py)
DISCOVER_SOCKETS = (socket_name("claude"), CREATE_SOCKET)
_FIELD_SEP = "\x1f"

_LIST_FORMAT = _FIELD_SEP.join(
    [
        "#{session_name}",
        "#{session_created}",
        "#{session_activity}",
        "#{session_attached}",
        "#{pane_pid}",
        "#{pane_current_path}",
        "#{session_group}",
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
    group: str = ""  # tmux session group, if any (named after its first member)

    @property
    def is_view(self) -> bool:
        """A grouped session that isn't its group's leader — one of the
        '<name>--term' terminal-tab views ensure_window_view creates. It
        shares the leader's windows and directory but hosts nothing of its
        own, so it must never be mistaken for a Claude session's home."""
        return bool(self.group) and self.group != self.name

    @property
    def key(self) -> str:
        """Unique across sockets (names may repeat between servers)."""
        return f"{self.socket}:{self.name}"


# tmux commands that DO something (vs the read-only polling volume of
# capture-pane / list-* / show-*, which would drown the log). Every one of
# these is a state change worth a debug-log line.
_MUTATING_COMMANDS = {
    "new-session", "new-window", "kill-session", "kill-window", "kill-pane",
    "select-window", "select-pane", "respawn-pane", "respawn-window",
    "split-window", "resize-pane", "send-keys", "paste-buffer", "swap-pane",
    "break-pane", "bind", "unbind", "set-hook", "set-option", "set-environment",
}


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
        # CAGENTS_SESSION_ID per live tmux session, keyed by (socket, name,
        # created). It's one `show-environment` subprocess per session per
        # list — dozens of spawns every 2s tick — for a value fixed at
        # new-session time. A killed-and-recreated session gets a new
        # `created` stamp, so a stale name never returns a stale id.
        self._env_cache: dict[tuple[str, str, float], str] = {}

    def _run(self, socket: str, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
        if args and args[0] in _MUTATING_COMMANDS:
            from .ctx import _log

            _log(f"tmux[{socket}]: {' '.join(args)}")
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
            if len(parts) != 7:
                continue
            name, created, activity, attached, pane_pid, pane_path, group = parts
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
                    group=group,
                )
            except ValueError:
                continue
        found = list(sessions.values())
        for sess in found:
            key = (socket, sess.name, sess.created)
            if key not in self._env_cache:
                self._env_cache[key] = self.get_session_env(
                    sess.name, "CAGENTS_SESSION_ID", socket=socket
                )
            sess.cagents_session_id = self._env_cache[key]
        live = {(socket, s.name, s.created) for s in found}
        for key in [k for k in self._env_cache if k[0] == socket and k not in live]:
            del self._env_cache[key]
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
        """The visible tail of a session's active pane, for prompt detection.

        -J rejoins any soft-wrapped lines back into one logical line
        first — without it, a resized terminal (or just a narrower one)
        can split a single status/spinner line ("Baking… (45s · stats)")
        across two physical lines at an arbitrary point, and every
        pane-text pattern in tmuxctl.py assumes its markers sit on one
        line. This makes the whole detection layer width-agnostic instead
        of quietly depending on whatever width the pane happened to be
        at capture time (confirmed live: without -J, a narrow pane splits
        exactly this way)."""
        socket = socket or self.create_socket
        try:
            # "=name:" — exact session match, default window/pane. A bare
            # "=name" resolves for attach/has-session but NOT for pane
            # targets (verified against tmux 3.6a).
            proc = self._run(
                socket, "capture-pane", "-p", "-J", "-t", f"={session_name}:", "-S", f"-{lines}"
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
        # Mouse wheel should scroll these sessions when previewed; and their
        # own status bar stays off — the workspace's tab bar is the chrome.
        if self.create_socket not in self._mouse_enabled:
            self._run(self.create_socket, "set", "-g", "mouse", "on")
            self._run(self.create_socket, "set", "-g", "status", "off")
            self._mouse_enabled.add(self.create_socket)
        return name

    def new_shell_session(
        self, directory: str, session_id: str = "", extra_env: list[str] | None = None
    ) -> str:
        """Create a detached session running a plain interactive shell (no
        claude) — for the "new conversation" terminal: the user picks a
        directory and types `claude` themselves, rather than cagents
        dictating the exact command line up front. Same socket/env/mouse
        setup as new_claude_session, minus the claude invocation itself.

        `extra_env` is typically the app's claude-shim PATH/ZDOTDIR `-e`
        pairs, so that `claude` typed in this shell is intercepted into a
        managed spawn the same way it already is everywhere else the shim
        applies — this method has no opinion on that, it just forwards
        whatever env the caller wants set at creation time."""
        name = self._unique_name(Path(directory).name)
        env_args: list[str] = list(extra_env or ())
        if session_id:
            env_args += ["-e", f"CAGENTS_SESSION_ID={session_id}"]
        proc = self._run(
            self.create_socket,
            "new-session", "-d", *env_args, "-s", name, "-c", directory,
            timeout=10.0,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {proc.stderr.strip() or 'unknown error'}")
        if session_id:
            self._run(
                self.create_socket,
                "set-environment", "-t", f"={name}", "CAGENTS_SESSION_ID", session_id,
            )
        if self.create_socket not in self._mouse_enabled:
            self._run(self.create_socket, "set", "-g", "mouse", "on")
            self._run(self.create_socket, "set", "-g", "status", "off")
            self._mouse_enabled.add(self.create_socket)
        return name

    def send_shell_command(self, session_name: str, command: str, socket: str | None = None) -> None:
        """Type a compound shell command line into a plain shell pane
        (new_shell_session's) and press Enter — the "seed" step: defines
        the recent-directory shortcut aliases and prints the menu. Not
        send_text's paste-buffer + delayed Enter dance (that exists to
        dodge Claude's own bracketed-paste UI timing); a single-line
        semicolon-joined shell command has no such concern."""
        socket = socket or self.create_socket
        proc = self._run(socket, "send-keys", "-t", f"={session_name}:", command, "Enter")
        if proc.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {proc.stderr.strip()}")

    def ensure_session_window(
        self, session_name: str, window_name: str, directory: str, socket: str | None = None
    ) -> None:
        """Add `window_name` to an already-running session if it doesn't
        have one yet — gives a live claude session its own persistent
        terminal window, without disturbing window 0 (the claude pane
        itself). Raises rather than silently no-op'ing when the session
        itself has vanished, so a caller never mistakes "nothing to
        attach to anymore" for "already set up"."""
        socket = socket or self.create_socket
        try:
            proc = self._run(socket, "list-windows", "-t", f"={session_name}", "-F", "#{window_name}")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"tmux list-windows failed: {error}")
        if proc.returncode != 0:
            raise RuntimeError(f"tmux session '{session_name}' not found")
        if window_name in proc.stdout.split():
            return
        result = self._run(
            socket, "new-window", "-d", "-t", f"={session_name}:", "-n", window_name, "-c", directory
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux new-window failed: {result.stderr.strip()}")

    def ensure_window_view(
        self, session_name: str, window_name: str, socket: str | None = None,
        force_select: bool = False,
    ) -> str:
        """A grouped session pinned to `window_name` of `session_name`.

        tmux's "current window" is a per-*session* attribute shared by
        every client attached to it — without this, a second client
        attaching to view `window_name` would drag the first client's
        view (e.g. the live claude pane) to whatever window it last
        selected. A session group shares the same windows/panes but
        tracks its own current window per grouped session, so the two
        views stay independent. Returns the grouped session's name."""
        socket = socket or self.create_socket
        group_name = f"{session_name}--{window_name}"
        created = False
        if not self.has_session(group_name, socket=socket):
            proc = self._run(
                socket, "new-session", "-d", "-s", group_name, "-t", f"={session_name}"
            )
            if proc.returncode != 0:
                raise RuntimeError(f"tmux new-session (group) failed: {proc.stderr.strip()}")
            created = True
        # Select only on creation (the group starts on the target session's
        # current window) or when the caller explicitly opens the view.
        # The passive per-refresh sync must NOT re-select: it spammed a
        # select-window every 2s and would forcibly snap the view back if
        # the user ever changed windows inside the nested client.
        if created or force_select:
            self._run(socket, "select-window", "-t", f"={group_name}:{window_name}")
        return group_name

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
        "status-right": " ← back · C-t shell · C-d diff ",
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
    "trust this folder",  # workspace trust dialog (observed live, v2.1.235)
    # AskUserQuestion's own question text is model-composed, not from a
    # fixed phrase list ("Do you prefer tabs or spaces?" etc.) — it will
    # never match any marker above. Its menu footer is the one constant
    # (confirmed live, v2.1.236), and it's also present on the plain
    # permission-dialog footer ("Esc to cancel · Tab to amend · ..."), so
    # this one marker covers both without needing a separate check.
    "Esc to cancel",
)

# The selection cursor on a numbered option — the signature of an actual
# dialog. Claude's *conversation text* frequently contains phrases like
# "Do you want me to…", so a phrase alone must never count as a prompt.
_CHOICE_ROW = re.compile(r"❯\s*\d+\.")

# How far back a dialog's choice-row + marker are trusted as CURRENT.
# Real bug, confirmed live: answering a dialog (most reproducibly the
# "do you trust this folder" workspace-trust prompt, since it's the one
# nearly everyone hits) leaves its own already-resolved ❯-cursored text
# sitting in the pane's scrollback — pane_shows_prompt used to search the
# WHOLE captured pane, so that stale text kept reading as "still needs
# you" well after the real state had moved on, until enough new output
# finally scrolled it out of the capture window. Same class of bug
# pane_shows_working's tail restriction already guards against, just
# never applied here.
_PROMPT_TAIL_LINES = 15

# Patterns that mean Claude is actively running a turn. The spinner hint
# text varies by UI state ("esc to interrupt", "· 1 shell still running",
# …) — observed live against v2.1.235. Kept as exact-phrase markers, so
# they're only ever checked against the single actual last line (see
# pane_shows_working) — matching them anywhere wider risks the same
# false-positive class this whole tail-restriction exists to prevent.
_WORKING_MARKERS = (
    "esc to interrupt",
    "ctrl+b to run in background",
)

# "N shell(s) still running" is the mid-turn spinner's own phrasing — a
# shell tool call blocks the turn, so seeing it always means WORKING (see
# test_shell_count_defers_to_the_mid_turn_still_running_marker). Deliberately
# scoped to "shell(s)": real bug, confirmed live — a plain "still running"
# substring check also matched the unrelated idle footer "N monitors still
# running" (a persistent Monitor watch, not an active turn), which read a
# worktree-creation session with only monitors going as WORKING. Monitors
# outlive the turn that started them by design (see MONITORING/
# monitor_running) and must never be conflated with the shell case.
_SHELL_STILL_RUNNING_RE = re.compile(r"\bshells?\s+still running\b")

# Newer Claude Code builds (v2.1.236+, observed live) replaced the spinner
# line's "(esc to interrupt)" suffix with elapsed time / token stats, e.g.
# "· Baking… (2m 34s · ↓ 10.0k tokens)" — no _WORKING_MARKERS text at all.
# The one constant is grammatical: an in-progress verb always ends in the
# single ellipsis char "…" right before the parenthetical; the *finished*
# form sitting in scrollback afterwards is past tense with no ellipsis
# ("Baked for 12m 53s"). This line can sit several rows above the actual
# last line (an idle input box + footer render below it while streaming),
# so it needs a wider tail than the exact-phrase markers above — still far
# too structurally specific to a real spinner to be faked by ordinary
# conversation text.
_SPINNER_IN_PROGRESS_RE = re.compile(r"\w+…\s*\(")
_SPINNER_TAIL_LINES = 10

# Live shell-count indicator in the idle footer, e.g. "· 1 shell running"
# — deliberately does NOT match "still running" (that phrasing is the
# mid-turn spinner's own _WORKING_MARKERS entry above, and always wins
# first; by the time this is checked the turn has already ended).
_SHELL_COUNT_RE = re.compile(r"(\d+)\s+shells?(?:\s+running)?\b")


def _tail_lines(pane_text: str, count: int) -> str:
    """The last `count` non-blank lines — the live status/footer area.

    Never search the *whole* pane for a live-status phrase: ordinary
    conversation scrollback can innocently contain the exact same words
    (a user literally asking "what is still running?"), which reads as a
    false live spinner if matched anywhere in the capture. Only the tail
    is ever actually live status text."""
    lines = [line for line in pane_text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def pane_shows_prompt(pane_text: str) -> bool:
    """True only for a real, CURRENT dialog: a ❯-cursored numbered choice
    AND a prompt phrase, both within the last _PROMPT_TAIL_LINES —
    checked in the tail for the same reason pane_shows_working is:
    either alone is too easy to fake with ordinary conversation output,
    and searching the whole pane risks matching an already-answered
    dialog's own text still sitting in scrollback."""
    tail = _tail_lines(pane_text, _PROMPT_TAIL_LINES)
    if not _CHOICE_ROW.search(tail):
        return False
    return any(marker in tail for marker in _PROMPT_MARKERS)


def pane_shows_working(pane_text: str) -> bool:
    """True when the live status line shows an in-progress spinner.

    Two checks, deliberately different widths: the exact-phrase markers
    only count on the single actual last line (an idle footer can trail
    below the spinner while streaming, so "last line" isn't always the
    spinner itself — but widening this check re-admits the false positive
    it exists to prevent, e.g. old scrollback literally asking "what is
    still running?"). The structural "Verb…  (" pattern is specific enough
    to safely check a wider tail, which is what actually catches it once
    a footer pushes the spinner line up."""
    tail = _tail_lines(pane_text, 1)
    if any(marker in tail for marker in _WORKING_MARKERS):
        return True
    if _SHELL_STILL_RUNNING_RE.search(tail):
        return True
    return bool(_SPINNER_IN_PROGRESS_RE.search(_tail_lines(pane_text, _SPINNER_TAIL_LINES)))


def pane_shell_count(pane_text: str) -> int:
    """How many shells the live status/footer says are still running right
    now (0 if none) — checked in the same narrow tail window as
    pane_shows_working, for the same reason."""
    match = _SHELL_COUNT_RE.search(_tail_lines(pane_text, 2))
    return int(match.group(1)) if match else 0


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
