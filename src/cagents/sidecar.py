"""Sidecar: cagents as a rail beside one persistent viewer pane.

The design invariant that makes everything fall out: **the right pane is
always just a tmux client — never a re-implementation.** Browsing shows
the highlighted session there; if it isn't already live, browsing to it
resumes the real `claude --resume` CLI right then (lazily — only the one
session you're actually looking at, not every one you scroll past) and
shows THAT, pixel-identical, because it IS Claude Code. There is no
separate "preview" renderer for dead sessions — an earlier version had
one (a static, non-interactive dump of the transcript text), which
violated this exact invariant and looked nothing like the real thing;
it's gone. Enter merely moves focus into the (already-live) pane; ←
moves focus back. Preview and attach cannot disagree because they are
the same thing.

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
from pathlib import Path

COLLAPSED_WIDTH = 34
WORK_SOCKET = "cagents-work"  # the tabbed workspace behind the right pane
TABS = ("session", "diff", "term-1", "+term")  # left-to-right (display names)
# Real window name for the "+term" tab. Not "+term" itself: tmux's target
# parser can't reliably select a window whose name starts with "+" (see
# _ensure_new_term_tab) — this is the name actually used for -t targeting
# and hook matching; "+term" is only ever the displayed label.
NEW_TERM_WINDOW = "newterm"
NEW_TERM_INDEX = 99  # pinned far right — new term-N windows insert before it


class Sidecar:
    def __init__(self, runner=None, own_pane: str = "", work_runner=None):
        # runner: outer-tmux (the container); work_runner: the workspace
        # server that holds the tabs. Both injectable for tests.
        self._run = _logged_runner("outer", runner or _outer_tmux)
        self._work = _logged_runner("work", work_runner or _work_tmux)
        self.term_env: list[str] = []  # -e PATH=... for terminal tabs (claude shim)
        self.own_pane = own_pane or os.environ.get("TMUX_PANE", "")
        self.pane_id: str = ""  # the viewer pane on the right
        self.current_command: str = ""  # what the session tab currently runs
        self.current_terminal_command: str = ""  # what the term-1 tab currently runs

    @staticmethod
    def enabled() -> bool:
        """Sidecar is the default whenever we're inside tmux — the container
        (CAGENTS_SIDECAR=1) or the user's own session both get pane-splitting
        attach. CAGENTS_SIDECAR=0 (the --fullscreen flag) opts out."""
        if os.environ.get("CAGENTS_SIDECAR") == "0":
            return False
        return bool(os.environ.get("TMUX"))

    # -- the tabbed workspace ---------------------------------------------------
    #
    # The right pane permanently attaches the "work" session on a private
    # socket; that session's WINDOWS are the tabs (native tmux tab bar at
    # the top of the pane, clickable): session | diff | term-1. Content
    # switches by respawning a window's pane; shells persist across tab
    # switches because the windows never die with the view.

    def ensure_workspace(
        self, terminal_dir: str = "", ctx_prog: str = "", context_path: str = "",
        shim_env: list[str] | None = None,
    ) -> None:
        """Create the work session + default tabs (idempotent), and make
        sure the right pane is attached to it. With ctx_prog given, clicking
        the diff tab rebuilds the diff, and clicking the "+term" tab (real
        window name "newterm" — see NEW_TERM_WINDOW) opens a fresh terminal
        tab (both after-select-window hooks).

        Options/hooks/structural tabs are (re-)applied every call, not just
        on first creation — the work session deliberately survives a
        cagents restart (terminal tabs persist), so a version upgrade that
        adds a tab or a hook must still reach a workspace that already
        existed before that upgrade shipped."""
        if shim_env is not None:
            self.term_env = shim_env
        created = False
        try:
            self._work(["has-session", "-t", "=work"])
        except RuntimeError:
            created = True
            self._work(["new-session", "-d", "-s", "work", "-n", "session",
                        _placeholder("select a session in the list")])

        self._ensure_window(
            "diff", command=_placeholder("C-d builds the diff for the selected session")
        )
        self._ensure_window("term-1", cwd=terminal_dir if created else "")
        self._ensure_new_term_tab()

        # This server exists solely for the workspace, so every option is
        # global (-g). (Window options like window-status-format only hit
        # the active window when given a session target.) set/set-hook are
        # idempotent — safe to repeat every call.
        for option in (
            ["set", "-g", "mouse", "on"],
            ["set", "-g", "escape-time", "10"],
            ["set", "-g", "status", "on"],
            ["set", "-g", "status-position", "top"],
            ["set", "-g", "status-style", "bg=colour236,fg=colour248"],
            ["set", "-g", "status-left", ""],
            ["set", "-g", "status-right", ""],
            ["set", "-g", "window-status-format", "  #W  "],
            ["set", "-g", "window-status-current-format",
             "#[bg=colour31,fg=colour231,bold]  #W  #[default]"],
            ["set", "-g", "window-status-separator", ""],
            # Mouse wheel over the tab bar must NOT switch tabs: tmux's
            # default WheelUp/DownStatus root binds fire previous/next-window,
            # and the tab bar sits exactly where you scroll while reading the
            # claude pane — a drifted wheel tick read as a phantom tab switch
            # (diagnosed from ctx.log, 2026-08-23). Clicks still switch tabs.
            ["unbind", "-n", "WheelUpStatus"],
            ["unbind", "-n", "WheelDownStatus"],
        ):
            self._work(option)

        if ctx_prog and context_path:
            import shlex

            q_context = shlex.quote(context_path)
            rebuild_diff = f"run-shell -b \"{ctx_prog} diff --context {q_context} --no-select\""
            new_term = f"run-shell -b \"{ctx_prog} newterm --context {q_context}\""
            # Clicking the term-1 tab is pure tmux — it never touches the
            # Python app — so this hook is what actually re-scopes the
            # terminal to the selected session on a click, same as "N"
            # (action_open_terminal) does for a keypress. --no-select:
            # the click already selected the window.
            open_term = f"run-shell -b \"{ctx_prog} shell --context {q_context} --no-select\""
            self._work([
                "set-hook", "-g", "after-select-window",
                f"if -F '#{{==:#{{window_name}},diff}}' '{rebuild_diff}' ''",
            ])
            self._work([
                "set-hook", "-g", "-a", "after-select-window",
                f"if -F '#{{==:#{{window_name}},{NEW_TERM_WINDOW}}}' '{new_term}' ''",
            ])
            self._work([
                "set-hook", "-g", "-a", "after-select-window",
                f"if -F '#{{==:#{{window_name}},term-1}}' '{open_term}' ''",
            ])
            # ---- forensic instrumentation (all via cagents-ctx wlog, which
            # timestamps into ctx.log — echo/date quoting inside tmux hooks
            # proved unfixable). Cross-validation for phantom tab switches:
            # every select-window command (even re-selecting the current
            # window), every actual window change, and every mouse event on
            # the status line gets a line.
            def wlog(tag: str) -> str:
                return f"{ctx_prog} wlog {tag} --context {q_context}"

            self._work([
                "set-hook", "-g", "-a", "after-select-window",
                'run-shell -b "' + wlog("select-window:#{window_name}") + '"',
            ])
            self._work([
                "set-hook", "-g", "session-window-changed",
                'run-shell -b "' + wlog("window-changed:#{window_name}") + '"',
            ])
            # Wheel over the tab bar: deliberately does NOT switch tabs
            # (phantom-switch suspect #1) — log-only so we see it happen.
            for key, tag in (
                ("WheelUpStatus", "wheel-up-status"),
                ("WheelDownStatus", "wheel-down-status"),
            ):
                self._work(["bind", "-n", key, "run-shell", "-b", wlog(tag)])
            # Tab-bar click: keep the default switch, but log it first with
            # coordinates — a phantom switch with no matching click line is
            # programmatic; one WITH a click line at odd coordinates points
            # at garbled mouse-escape parsing.
            click_log = wlog("status-click:#{mouse_x},#{mouse_y}:#{window_name}")
            self._work([
                "bind", "-n", "MouseDown1Status",
                'run-shell -b "' + click_log + '" ; switch-client -t =',
            ])
        if created:
            self._work(["select-window", "-t", "=work:session"])
        self._ensure_viewer_pane()

    def _ensure_new_term_tab(self) -> None:
        """The "new terminal" tab, pinned at the far right (a fixed high
        index) so freshly created term-N windows always insert before it.

        Named "newterm", NOT "+term": confirmed live that tmux's target
        parser (`-t work:+term`) silently fails to select a window whose
        name starts with "+" — select-window returns success but the
        current window never actually changes, so the after-select-window
        hook this tab depends on never fires. The window is real-named
        "newterm" and DISPLAYS as "+term" via a per-window
        window-status-format override instead."""
        try:
            windows = self._work(["list-windows", "-t", "=work", "-F", "#W"]).split()
        except RuntimeError:
            windows = []
        if NEW_TERM_WINDOW in windows:
            return
        self._work([
            "new-window", "-d", "-t", f"=work:{NEW_TERM_INDEX}", "-n", NEW_TERM_WINDOW,
            _placeholder("select this tab to open a new terminal"),
        ])
        for fmt in ("window-status-format", "window-status-current-format"):
            self._work(["set-window-option", "-t", f"=work:{NEW_TERM_WINDOW}", fmt, "  +term  "])

    def _ensure_viewer_pane(self) -> None:
        """The container's right pane runs one thing, forever: a client
        attached to the workspace."""
        if self.pane_id and self._pane_alive():
            return
        attach = f"env -u TMUX tmux -L {WORK_SOCKET} attach-session -t '=work'"
        out = self._run(
            ["split-window", "-h", "-d", "-P", "-F", "#{pane_id}",
             "-t", self.own_pane, attach]
        )
        self.pane_id = out.strip()
        if self.own_pane:
            self._run(["resize-pane", "-t", self.own_pane, "-x", "50%"])

    def _ensure_window(self, name: str, command: str = "", cwd: str = "") -> None:
        try:
            windows = self._work(["list-windows", "-t", "=work", "-F", "#W"]).split()
        except RuntimeError:
            windows = []
        if name in windows:
            return
        args = ["new-window", "-d", "-t", "=work:", "-n", name]
        if name.startswith("term"):
            args += self.term_env
        if cwd:
            args += ["-c", cwd]
        if command:
            args.append(command)
        self._work(args)

    def show_viewer(self, shell_command: str) -> None:
        """Point the SESSION TAB at `shell_command` (live attach or static
        preview). Never steals focus or switches tabs — browsing must not
        disturb what you're looking at. Content is respawned BEFORE the
        viewer pane is (first) created: at startup the split must appear
        already showing the conversation, not flash the placeholder shell
        for a beat (user-reported)."""
        if shell_command != self.current_command:
            self._ensure_window("session")
            self._work(["respawn-pane", "-k", "-t", "=work:session", shell_command])
            self.current_command = shell_command
        self._ensure_viewer_pane()

    def select_tab(self, name: str) -> None:
        self._work(["select-window", "-t", f"=work:{name}"])

    def open_diff_tab(self, pager_command: str) -> None:
        """Fresh diff in the diff tab, and switch to it."""
        self._ensure_window("diff")
        self._work(["respawn-pane", "-k", "-t", "=work:diff", pager_command])
        self.select_tab("diff")

    def sync_terminal_tab(self, shell_command: str) -> None:
        """Keep the term-1 PANE pointed at `shell_command` — normally a
        nested attach into the currently selected session's OWN terminal
        window (see app.py's _sync_terminal) — without switching tabs or
        stealing focus. Mirrors show_viewer for the session tab: this
        runs on every selection change (not just when you explicitly open
        the tab), so browsing the list while sitting on the terminal tab
        actually follows the highlight instead of showing one shell
        shared by every session. Only respawns when the target actually
        changed, so revisiting the same session's terminal doesn't kill
        work already running in it."""
        self._ensure_window("term-1")
        if shell_command != self.current_terminal_command:
            self._work(["respawn-pane", "-k", "-t", "=work:term-1", shell_command])
            self.current_terminal_command = shell_command

    def open_terminal_tab(self, shell_command: str) -> None:
        """Point the terminal tab at `shell_command` and switch to it —
        the explicit "N"/click entry point. See sync_terminal_tab for the
        passive, browsing-follows-selection half of this."""
        self.sync_terminal_tab(shell_command)
        self.select_tab("term-1")

    def focus_session(self) -> None:
        """Enter: the session tab, focused."""
        self.select_tab("session")
        if self.pane_id:
            self._run(["select-pane", "-t", self.pane_id])

    def focus_pane(self) -> None:
        """Focus the workspace pane without changing which tab is active."""
        if self.pane_id:
            self._run(["select-pane", "-t", self.pane_id])

    def focus_rail(self) -> None:
        if self.own_pane:
            self._run(["select-pane", "-t", self.own_pane])

    def hide_rail(self) -> None:
        """Zoom the viewer to full width and focus it."""
        if self.pane_id and self._pane_alive():
            self._run(["select-pane", "-t", self.pane_id])
            self._run(["resize-pane", "-Z", "-t", self.pane_id])

    def _pane_alive(self) -> bool:
        if not self.pane_id:
            return False
        try:
            out = self._run(["list-panes", "-F", "#{pane_id}"])
        except RuntimeError:
            return False
        return self.pane_id in out.split()


def _work_tmux(args: list[str]) -> str:
    """Talk to the workspace server (the tabs). $TMUX must be stripped:
    we're inside the container, and tmux refuses new-session (even -d)
    when it thinks we're nesting."""
    env = os.environ.copy()
    env.pop("TMUX", None)
    proc = subprocess.run(
        ["tmux", "-L", WORK_SOCKET, *args],
        capture_output=True, text=True, timeout=10, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"tmux {args[0]} failed")
    return proc.stdout


def _placeholder(message: str) -> str:
    import shlex

    inner = f'printf "\\n  %s\\n" {shlex.quote(message)}; exec sleep 2147483647'
    return "sh -c " + shlex.quote(inner)


def _logged_runner(tag: str, fn):
    """Wrap a tmux runner so every mutating command lands in ctx.log —
    the cross-validation timeline for 'the app switched my tab' reports."""
    from .tmuxctl import _MUTATING_COMMANDS

    def run(args: list[str]) -> str:
        if args and args[0] in _MUTATING_COMMANDS:
            from .ctx import _log

            _log(f"tmux[{tag}]: {' '.join(str(a) for a in args)}")
        return fn(args)

    return run


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


# ------------------------------------------------------- the container --

CONTAINER_SOCKET = "cagents-ui"
CONTAINER_SESSION = "cagents"

# Focus-driven rail width: wide while choosing, slim while working.
_FOCUS_HOOK = (
    "if -F '#{==:#{pane_index},0}' "
    "'resize-pane -t :.0 -x 50%' "
    f"'resize-pane -t :.0 -x {COLLAPSED_WIDTH}'"
)

# The arrow keys are a size control for the Claude pane, over three states
# ordered by its width: WIDE (50/50, rail focused) < SMALL (slim rail,
# session focused) < HIDDEN (rail zoomed away). ← shrinks it one step,
# → grows it one step; both saturate at the ends.
#
# From inside the session pane:
#   ← : HIDDEN -> SMALL (unzoom, stay in the session)
#       SMALL  -> WIDE  (focus the rail; the hook widens it)
#   → : SMALL  -> HIDDEN (zoom)
#       HIDDEN -> passes through to Claude (already max)
# From the rail, both pass through to the app (kanban columns; → also
# does WIDE -> SMALL by focusing the session).
_LEFT_CYCLE = [
    "bind", "-n", "Left",
    "if", "-F", "#{==:#{pane_index},0}",
    "send-keys Left",
    "if -F '#{window_zoomed_flag}' "
    "'resize-pane -Z -t :.1 ; select-pane -t :.1' "
    "'select-pane -t :.0'",
]

_RIGHT_CYCLE = [
    "bind", "-n", "Right",
    "if", "-F", "#{==:#{pane_index},0}",
    "send-keys Right",
    "if -F '#{window_zoomed_flag}' "
    "'send-keys Right' "
    "'resize-pane -Z -t :.1'",
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
        ["set", "-g", "status-right", " ←/→ size · C-d diff tab · C-t terminal tab "],
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
        return [_LEFT_CYCLE, _RIGHT_CYCLE]
    return [["unbind", "-n", "Left"], ["unbind", "-n", "Right"]]


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
    """C-t (shell in the session's dir) and C-d (diff vs master popup),
    C-t rather than C-s so Claude Code's own ctrl-s binding stays reachable;
    available regardless of which pane has focus."""
    import shlex

    prog = shlex.quote(ctx_prog)
    ctx = shlex.quote(context_path)
    return [
        # Drop the pre-rename C-s bind a persisted container may still carry.
        ["unbind", "-n", "C-s"],
        ["bind", "-n", "C-t", "run-shell", "-b", f"{prog} shell --context {ctx}"],
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
    term = os.environ.get("CAGENTS_TERM_PROGRAM", "")
    env = f"CAGENTS_SIDECAR=1 CAGENTS_LAUNCH_CWD={shlex.quote(launch_cwd)} "
    if term:
        env += f"CAGENTS_TERM_PROGRAM={shlex.quote(term)} "
    return env + " ".join(shlex.quote(p) for p in parts)


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
        # a stale workspace from a dead container would show ghost tabs
        subprocess.run(["tmux", "-L", WORK_SOCKET, "kill-server"], capture_output=True)
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
