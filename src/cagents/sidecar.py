"""Sidecar: cagents as a rail beside one persistent viewer pane.

The design invariant that makes everything fall out: **the right pane is
always just a tmux client (or a static transcript render) — never a
re-implementation.** Browsing shows the highlighted session there (a real
attach, pixel-identical because it IS Claude Code); Enter merely moves
focus into it; ← moves focus back. Preview and attach cannot disagree
because they are the same thing.

Layout is a 3-state cycle on ←:

    SMALL (session focused, 34-col rail)
      ← -> WIDE   (50/50, rail focused — "back to the list")
      ← -> HIDDEN (rail zoomed away, session full-width)   [app-driven]
      ← -> SMALL  ...

The SMALL->WIDE and HIDDEN->SMALL steps are pure tmux (root binding); the
WIDE->HIDDEN step is the app's (← reaches cagents when the rail has
focus, so views like kanban can consume it first).
"""

from __future__ import annotations

import os
import subprocess

COLLAPSED_WIDTH = 34


class Sidecar:
    def __init__(self, runner=None, own_pane: str = ""):
        # runner: (args: list[str]) -> str, injectable for tests.
        self._run = runner or _outer_tmux
        self.own_pane = own_pane or os.environ.get("TMUX_PANE", "")
        self.pane_id: str = ""  # the viewer pane on the right
        self.current_command: str = ""  # what the viewer currently runs

    @staticmethod
    def enabled() -> bool:
        """Sidecar is the default whenever we're inside tmux — the container
        (CAGENTS_SIDECAR=1) or the user's own session both get pane-splitting
        attach. CAGENTS_SIDECAR=0 (the --fullscreen flag) opts out."""
        if os.environ.get("CAGENTS_SIDECAR") == "0":
            return False
        return bool(os.environ.get("TMUX"))

    # -- the viewer pane -------------------------------------------------------

    def show_viewer(self, shell_command: str) -> None:
        """Point the right pane at `shell_command` (live attach or static
        preview). Never steals focus and never resizes — browsing must not
        disturb the layout."""
        if shell_command == self.current_command and self._pane_alive():
            return
        if self.pane_id and self._pane_alive():
            self._run(["respawn-pane", "-k", "-t", self.pane_id, shell_command])
        else:
            out = self._run(
                ["split-window", "-h", "-d", "-P", "-F", "#{pane_id}",
                 "-t", self.own_pane, shell_command]
            )
            self.pane_id = out.strip()
            if self.own_pane:
                # Fresh split: the rail keeps its browsing share.
                self._run(["resize-pane", "-t", self.own_pane, "-x", "50%"])
        self.current_command = shell_command

    def focus_session(self) -> None:
        if self.pane_id:
            self._run(["select-pane", "-t", self.pane_id])

    def focus_rail(self) -> None:
        if self.own_pane:
            self._run(["select-pane", "-t", self.own_pane])

    def hide_rail(self) -> None:
        """WIDE -> HIDDEN: zoom the viewer to full width and focus it."""
        if self.pane_id and self._pane_alive():
            self._run(["select-pane", "-t", self.pane_id])
            self._run(["resize-pane", "-Z", "-t", self.pane_id])

    def split_shell(self, directory: str) -> None:
        """A throwaway shell pane below the viewer, cwd'd into the
        worktree/project. Exits with the shell."""
        target = self.pane_id if (self.pane_id and self._pane_alive()) else self.own_pane
        args = ["split-window", "-v", "-l", "12", "-c", directory]
        if target:
            args += ["-t", target]
        self._run(args)

    def _pane_alive(self) -> bool:
        if not self.pane_id:
            return False
        try:
            out = self._run(["list-panes", "-F", "#{pane_id}"])
        except RuntimeError:
            return False
        return self.pane_id in out.split()


def _outer_tmux(args: list[str]) -> str:
    """Talk to the enclosing tmux server (resolved from $TMUX)."""
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"tmux {args[0]} failed")
    return proc.stdout


def nested_attach_command(socket: str, session_name: str) -> str:
    """The command the viewer pane runs for a live session: attach to it on
    its own socket, with $TMUX cleared so tmux allows the nesting."""
    safe = session_name.replace("'", "'\\''")
    return f"env -u TMUX tmux -L {socket} attach-session -t '={safe}'"


def program_invocation(extra_args: list[str]) -> list[str]:
    """argv that re-runs this cagents installation with extra_args."""
    import sys

    prog = os.path.abspath(sys.argv[0])
    if prog.endswith("__main__.py"):
        return [sys.executable, "-m", "cagents", *extra_args]
    return [prog, *extra_args]


def preview_command(session_id: str, store_path: str, claude_dir: str = "") -> str:
    """The viewer command for a dead session: print its transcript (real
    parse, ANSI colors) and hold the pane. Scrolling comes free from the
    outer tmux (wheel enters copy-mode over a quiet pane)."""
    import shlex

    args = ["--preview-session", session_id, "--store", store_path]
    if claude_dir:
        args += ["--claude-dir", claude_dir]
    return " ".join(shlex.quote(p) for p in program_invocation(args))


# ------------------------------------------------------- the container --

CONTAINER_SOCKET = "cagents-ui"
CONTAINER_SESSION = "cagents"

# Focus-driven rail width: wide while choosing, slim while working.
_FOCUS_HOOK = (
    "if -F '#{==:#{pane_index},0}' "
    "'resize-pane -t :.0 -x 50%' "
    f"'resize-pane -t :.0 -x {COLLAPSED_WIDTH}'"
)

# ← from inside the session pane: HIDDEN -> SMALL (unzoom, slim rail,
# stay in the session) or SMALL -> WIDE (focus the rail; the hook widens
# it). From the rail, ← passes through to the app (kanban columns, or the
# WIDE -> HIDDEN step).
_LEFT_CYCLE = [
    "bind", "-n", "Left",
    "if", "-F", "#{==:#{pane_index},0}",
    "send-keys Left",
    "if -F '#{window_zoomed_flag}' "
    "'resize-pane -Z -t :.1 ; select-pane -t :.1' "
    "'select-pane -t :.0'",
]


def container_setup_commands() -> list[list[str]]:
    """tmux commands that shape the container. Esc is deliberately NOT
    bound — inside a session it is Claude's interrupt key."""
    return [
        ["set", "-g", "escape-time", "10"],  # keep Esc snappy for Claude
        ["set", "-g", "mouse", "on"],  # click to focus; wheel scrolls the hovered pane
        # A slim statusline under everything showing the cagents keys.
        ["set", "-g", "status", "on"],
        ["set", "-g", "status-position", "bottom"],
        ["set", "-g", "status-style", "bg=colour235,fg=colour246"],
        ["set", "-g", "status-left", " cagents "],
        ["set", "-g", "status-left-style", "bg=colour31,fg=colour231,bold"],
        ["set", "-g", "status-right", " ← layout · C-s shell · C-d diff "],
        ["set", "-g", "status-right-length", "60"],
        ["set", "-g", "window-status-format", ""],
        ["set", "-g", "window-status-current-format", ""],
        ["set", "-g", "focus-events", "on"],
        ["set", "-g", "detach-on-destroy", "on"],
        # after-select-pane fires for keys AND mouse clicks (pane-focus-in
        # would need terminal focus reporting, which many terminals lack).
        ["set-hook", "-g", "after-select-pane", _FOCUS_HOOK],
    ]


def left_capture_commands(enable: bool) -> list[list[str]]:
    if enable:
        return [_LEFT_CYCLE]
    return [["unbind", "-n", "Left"]]


def apply_left_capture(enable: bool, runner=None) -> None:
    """Set/unset the ← layout binding on the enclosing container server."""
    run = runner or _outer_tmux
    for command in left_capture_commands(enable):
        try:
            run(command)
        except RuntimeError:
            if enable:
                raise  # failing to bind is worth surfacing; unbind noise isn't


def ctx_bind_commands(ctx_prog: str, context_path: str) -> list[list[str]]:
    """C-s (shell in the session's dir) and C-d (diff vs master popup),
    available regardless of which pane has focus."""
    import shlex

    prog = shlex.quote(ctx_prog)
    ctx = shlex.quote(context_path)
    return [
        ["bind", "-n", "C-s", "run-shell", "-b", f"{prog} shell --context {ctx}"],
        ["bind", "-n", "C-d", "run-shell", "-b", f"{prog} diff --context {ctx}"],
    ]


def apply_ctx_binds(ctx_prog: str, context_path: str, runner=None) -> None:
    run = runner or _outer_tmux
    for command in ctx_bind_commands(ctx_prog, context_path):
        run(command)


def self_command(argv: list[str]) -> str:
    """The shell command that re-runs this cagents with the same arguments
    inside the container pane."""
    import shlex

    parts = program_invocation(argv)
    launch_cwd = os.environ.get("CAGENTS_LAUNCH_CWD") or os.getcwd()
    return (
        f"CAGENTS_SIDECAR=1 CAGENTS_LAUNCH_CWD={shlex.quote(launch_cwd)} "
        + " ".join(shlex.quote(p) for p in parts)
    )


def _container_is_healthy(tmux: list[str]) -> bool:
    """An existing container session only counts if pane 0 still runs the
    cagents app. Otherwise it's an orphan (the app died, a session pane got
    renumbered into slot 0) and reattaching would trap the user in it."""
    proc = subprocess.run(
        [*tmux, "list-panes", "-t", f"={CONTAINER_SESSION}",
         "-F", "#{pane_index}\x1f#{pane_current_command}"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        index, _, command = line.partition("\x1f")
        if index == "0":
            return "python" in command.lower() or "cagents" in command.lower()
    return False


def bootstrap_container(argv: list[str]) -> "None":
    """Wrap this invocation in the persistent container: cagents in pane 0,
    the viewer to its right. Replaces the current process with
    `tmux attach` — never returns."""
    import shutil

    tmux = ["tmux", "-L", CONTAINER_SOCKET]
    has = subprocess.run(
        [*tmux, "has-session", "-t", f"={CONTAINER_SESSION}"], capture_output=True
    )
    if has.returncode == 0 and not _container_is_healthy(tmux):
        subprocess.run(
            [*tmux, "kill-session", "-t", f"={CONTAINER_SESSION}"], capture_output=True
        )
        has = subprocess.run(
            [*tmux, "has-session", "-t", f"={CONTAINER_SESSION}"], capture_output=True
        )
    if has.returncode != 0:
        size = shutil.get_terminal_size()
        subprocess.run(
            [*tmux, "new-session", "-d", "-s", CONTAINER_SESSION,
             "-x", str(size.columns), "-y", str(size.lines), self_command(argv)],
            check=True,
        )
        for command in container_setup_commands():
            subprocess.run([*tmux, *command], capture_output=True)
    os.execvp("tmux", [*tmux, "attach-session", "-t", f"={CONTAINER_SESSION}"])


def should_bootstrap(environ, stdout_is_tty: bool, fullscreen_flag: bool) -> bool:
    """Auto-wrap in the container when run bare in a real terminal."""
    if fullscreen_flag or not stdout_is_tty:
        return False
    if environ.get("TMUX"):  # already inside tmux: sidecar splits in place
        return False
    return environ.get("CAGENTS_SIDECAR") != "1"
