"""Sidecar mode: cagents as a persistent left rail.

When cagents runs inside a tmux container (the `cagents-shell` script,
which sets CAGENTS_SIDECAR=1), attaching stops meaning "hand over the
whole terminal". Instead the session opens in a pane to the right — a
nested attach onto the user's existing `claude` socket — and cagents
stays alive on the left, collapsed to a slim rail, states still ticking.

Focus movement and rail width are the container's job (tmux hooks in
cagents-shell); this module only opens/replaces the right pane. All tmux
calls here talk to the *outer* server via $TMUX, never the claude socket.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger("cagents.sidecar")

COLLAPSED_WIDTH = 34


class Sidecar:
    def __init__(self, runner=None, own_pane: str = ""):
        # runner: (args: list[str]) -> str, injectable for tests.
        self._run = runner or _outer_tmux
        self.own_pane = own_pane or os.environ.get("TMUX_PANE", "")
        self.pane_id: str = ""  # the session pane we manage on the right

    @staticmethod
    def enabled() -> bool:
        """Sidecar is the default whenever we're inside tmux — the container
        (CAGENTS_SIDECAR=1) or the user's own session both get pane-splitting
        attach. CAGENTS_SIDECAR=0 (the --fullscreen flag) opts out."""
        if os.environ.get("CAGENTS_SIDECAR") == "0":
            return False
        return bool(os.environ.get("TMUX"))

    def open(self, shell_command: str) -> None:
        """Run `shell_command` in the right pane (creating or replacing it),
        collapse the rail, and move focus to the session."""
        self._spawn(shell_command)
        if self.own_pane:
            self._run(["resize-pane", "-t", self.own_pane, "-x", str(COLLAPSED_WIDTH)])
        self._run(["select-pane", "-t", self.pane_id])

    def preview(self, shell_command: str) -> None:
        """Keep the right pane on a live read-only view of `shell_command`
        (a real Claude Code render, not a re-implementation) as selection
        moves — without stealing focus from the list."""
        self._spawn(shell_command)

    def _spawn(self, shell_command: str) -> None:
        if self.pane_id and self._pane_alive():
            self._run(["respawn-pane", "-k", "-t", self.pane_id, shell_command])
        else:
            out = self._run(
                ["split-window", "-h", "-d", "-P", "-F", "#{pane_id}",
                 "-t", self.own_pane, shell_command]
            )
            self.pane_id = out.strip()

    def split_shell(self, directory: str) -> None:
        """A throwaway shell pane below the session (or the rail), cwd'd
        into the worktree/project. Exits with the shell."""
        target = self.pane_id if (self.pane_id and self._pane_alive()) else self.own_pane
        args = ["split-window", "-v", "-l", "12", "-c", directory]
        if target:
            args += ["-t", target]
        self._run(args)

    def open_command(self, shell_command: str) -> None:
        """Run an arbitrary full-screen tool (e.g. lazygit) in the right
        pane — same slot the session uses; ← closes it the same way."""
        self.open(shell_command)

    def expand(self, width: str = "50%") -> None:
        """Grow the rail back out (used when no container hook will)."""
        if self.own_pane:
            self._run(["resize-pane", "-t", self.own_pane, "-x", width])

    def _pane_alive(self) -> bool:
        try:
            out = self._run(["list-panes", "-F", "#{pane_id}"])
        except RuntimeError:
            return False
        return self.pane_id in out.split()


def _outer_tmux(args: list[str]) -> str:
    """Talk to the enclosing tmux server (resolved from $TMUX)."""
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        error = proc.stderr.strip() or f"tmux {args[0]} failed"
        logger.warning("outer tmux %r failed: %s", args, error)
        raise RuntimeError(error)
    return proc.stdout


def nested_attach_command(socket: str, session_name: str, read_only: bool = False) -> str:
    """The command the right pane runs: attach to the claude-socket session,
    with $TMUX cleared so tmux allows the (deliberate) nesting. `read_only`
    gives a live, look-but-don't-touch view (used for the passive preview;
    a real attach never passes it)."""
    safe = session_name.replace("'", "'\\''")
    flag = "-r " if read_only else ""
    return f"env -u TMUX tmux -L {socket} attach-session {flag}-t '={safe}'"


# ------------------------------------------------------- the container --

CONTAINER_SOCKET = "cagents-ui"
CONTAINER_SESSION = "cagents"

# Focus-driven rail width: wide while choosing, slim while working.
_FOCUS_HOOK = (
    "if -F '#{==:#{pane_index},0}' "
    "'resize-pane -t :.0 -x 50%' "
    f"'resize-pane -t :.0 -x {COLLAPSED_WIDTH}'"
)


def container_setup_commands() -> list[list[str]]:
    """tmux commands that shape the container. Esc is deliberately NOT
    bound — inside a session it is Claude's interrupt key."""
    return [
        ["set", "-g", "escape-time", "10"],  # keep Esc snappy for Claude
        ["set", "-g", "mouse", "on"],  # click a pane to focus it
        # A slim statusline under everything showing the cagents keys.
        ["set", "-g", "status", "on"],
        ["set", "-g", "status-position", "bottom"],
        ["set", "-g", "status-style", "bg=colour235,fg=colour246"],
        ["set", "-g", "status-left", " cagents "],
        ["set", "-g", "status-left-style", "bg=colour31,fg=colour231,bold"],
        ["set", "-g", "status-right",
         " ← back · ctrl+\\ toggle · t shell · V diff · = expand "],
        ["set", "-g", "status-right-length", "70"],
        ["set", "-g", "window-status-format", ""],
        ["set", "-g", "window-status-current-format", ""],
        ["set", "-g", "focus-events", "on"],
        ["set", "-g", "detach-on-destroy", "on"],
        # after-select-pane fires for keys AND mouse clicks (pane-focus-in
        # would need terminal focus reporting, which many terminals lack).
        ["set-hook", "-g", "after-select-pane", _FOCUS_HOOK],
        # Back to the list / back to the session. M-q needs the terminal to
        # send Option/Alt as Meta; C-\\ works everywhere. The toggle goes
        # through select-pane (not last-pane) so the resize hook fires.
        ["bind", "-n", "M-q", "select-pane", "-t", ":.0"],
        ["bind", "-n", "M-w", "select-pane", "-t", ":.1"],
        ["bind", "-n", "C-\\",
         "if", "-F", "#{==:#{pane_index},0}",
         "select-pane -t :.1", "select-pane -t :.0"],
    ]


# Left-arrow capture: inside a session, Left closes the pane (the session
# keeps running in the background) and lands you back on the list. In the
# rail pane, Left passes through to cagents untouched. The trade-off: while
# the setting is on, Left no longer moves the cursor when editing text in
# the Claude prompt — Claude's own empty-prompt Left (the agents panel) is
# what it replaces.
_LEFT_CAPTURE = [
    "bind", "-n", "Left",
    "if", "-F", "#{==:#{pane_index},0}",
    "send-keys Left", "kill-pane -t :.1",
]


def left_capture_commands(enable: bool) -> list[list[str]]:
    if enable:
        return [_LEFT_CAPTURE]
    return [["unbind", "-n", "Left"]]


def apply_left_capture(enable: bool, runner=None) -> None:
    """Set/unset the Left binding on the enclosing container server. Only
    meaningful inside the container (callers check CAGENTS_SIDECAR)."""
    run = runner or _outer_tmux
    for command in left_capture_commands(enable):
        try:
            run(command)
        except RuntimeError:
            if enable:
                raise  # failing to bind is worth surfacing; unbind noise isn't


def self_command(argv: list[str]) -> str:
    """The shell command that re-runs this cagents with the same arguments
    inside the container pane."""
    import shlex
    import sys

    prog = os.path.abspath(sys.argv[0])
    if prog.endswith("__main__.py"):
        parts = [sys.executable, "-m", "cagents", *argv]
    else:
        parts = [prog, *argv]
    return "CAGENTS_SIDECAR=1 " + " ".join(shlex.quote(p) for p in parts)


def bootstrap_container(argv: list[str]) -> "None":
    """Wrap this invocation in the persistent container: cagents in pane 0,
    sessions opening to the right. Replaces the current process with
    `tmux attach` — never returns."""
    import shutil

    tmux = ["tmux", "-L", CONTAINER_SOCKET]
    has = subprocess.run(
        [*tmux, "has-session", "-t", f"={CONTAINER_SESSION}"], capture_output=True
    )
    if has.returncode != 0:
        cmd = self_command(argv)
        logger.info("no %r container session yet; creating one: %s", CONTAINER_SOCKET, cmd)
        size = shutil.get_terminal_size()
        subprocess.run(
            [*tmux, "new-session", "-d", "-s", CONTAINER_SESSION,
             "-x", str(size.columns), "-y", str(size.lines), cmd],
            check=True,
        )
        for command in container_setup_commands():
            subprocess.run([*tmux, *command], capture_output=True)
    else:
        logger.info("reusing existing %r container session", CONTAINER_SOCKET)
    logger.info("execvp tmux attach-session (this process ends here)")
    os.execvp("tmux", [*tmux, "attach-session", "-t", f"={CONTAINER_SESSION}"])


def should_bootstrap(environ, stdout_is_tty: bool, fullscreen_flag: bool) -> bool:
    """Auto-wrap in the container when run bare in a real terminal."""
    if fullscreen_flag or not stdout_is_tty:
        return False
    if environ.get("TMUX"):  # already inside tmux: sidecar splits in place
        return False
    return environ.get("CAGENTS_SIDECAR") != "1"
