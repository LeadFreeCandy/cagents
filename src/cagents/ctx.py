"""cagents-ctx — tmux-side helper for the global C-t / C-d keys, and for
tab-click hooks on the workspace (clicking a tab is pure tmux and never
touches the Python app process, so anything a click needs to do lives
here, not in app.py).

The app continuously writes the current session's directory (and, for
`shell`, which live tmux session it's scoped to) to a small context
file; tmux root bindings / hooks invoke this script, which reads it and
acts *inside the same tmux server* (run-shell provides $TMUX):

    cagents-ctx shell   --context <file>   the terminal tab (or a split) —
                                           THIS session's own worktree and
                                           its own persistent terminal
                                           window, never one shared with
                                           whichever session opened one
                                           last (see do_shell)
    cagents-ctx diff    --context <file>   the diff tab (or a popup)
    cagents-ctx event <Kind> --file <f>    Claude Code hook target: stamp a
                                           state event for the session

Kept dependency-free and instant — it runs on a keypress / hook.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .sockets import socket_name

CONTEXT_FILE = "context.json"
# Override for verifying this actual binary against an isolated tmux
# server instead of the real workspace — never set by cagents itself.
WORK_SOCKET = os.environ.get("CAGENTS_WORK_SOCKET_OVERRIDE") or socket_name("cagents-work")


def read_context(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_context(
    path: Path, directory: str, session_id: str,
    diff_mode: str = "branch", shim_dir: str = "",
    tmux_name: str = "", tmux_socket: str = "",
) -> None:
    payload = json.dumps(
        {"dir": directory, "session_id": session_id, "diff_mode": diff_mode,
         "shim_dir": shim_dir, "tmux_name": tmux_name, "tmux_socket": tmux_socket}
    )
    try:
        path.write_text(payload, "utf-8")
    except OSError:
        pass  # best effort; the keys just won't have a target


def _tmux(*args: str) -> int:
    return subprocess.run(["tmux", *args], capture_output=True, timeout=15).returncode


def _work(*args: str) -> int:
    return subprocess.run(
        ["tmux", "-L", WORK_SOCKET, *args], capture_output=True, timeout=15
    ).returncode


def _work_output(*args: str) -> str:
    proc = subprocess.run(
        ["tmux", "-L", WORK_SOCKET, *args], capture_output=True, text=True, timeout=15
    )
    return proc.stdout if proc.returncode == 0 else ""


def _workspace_alive() -> bool:
    return _work("has-session", "-t", "=work") == 0


def _work_windows() -> list[str]:
    proc = subprocess.run(
        ["tmux", "-L", WORK_SOCKET, "list-windows", "-t", "=work", "-F", "#W"],
        capture_output=True, text=True, timeout=15,
    )
    return proc.stdout.split() if proc.returncode == 0 else []


# Clicking the diff tab rebuilds via a tmux hook, and C-d both rebuilds and
# selects — which itself fires the hook. A timestamp in the work server's
# environment dedupes, so one keypress is one build, never a loop.
DIFF_DEDUPE_SECONDS = 2.0


def _recently_built(now=None) -> bool:
    import time

    proc = subprocess.run(
        ["tmux", "-L", WORK_SOCKET, "show-environment", "-g", "CAGENTS_DIFF_TS"],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0 or "=" not in proc.stdout:
        return False
    try:
        stamp = float(proc.stdout.strip().split("=", 1)[1])
    except ValueError:
        return False
    return ((now if now is not None else time.time()) - stamp) < DIFF_DEDUPE_SECONDS


def _mark_built() -> None:
    import time

    _work("set-environment", "-g", "CAGENTS_DIFF_TS", str(time.time()))


_LOG_FILE: Path | None = None
_LOG_FH = None  # cached append handle — reopening per line cost ~1ms on the UI thread
LOG_FILE_NAME = "ctx.log"


def init_log(state_dir: Path) -> None:
    """Point cagents-ctx logging at <state_dir>/ctx.log. The app calls this
    too, so hook processes and the app narrate into the same file."""
    global _LOG_FILE, _LOG_FH
    _LOG_FILE = state_dir / LOG_FILE_NAME
    if _LOG_FH is not None:
        try:
            _LOG_FH.close()
        except OSError:
            pass
        _LOG_FH = None


_VERSION_STAMP: str | None = None


def version_stamp() -> str:
    """'cagents <version> @<commit>[+dirty]' for THIS running code — a stale
    long-running app and a freshly installed hook can differ, and the log
    must show which code each line came from. Memoized: the git lookup
    runs once per process."""
    global _VERSION_STAMP
    if _VERSION_STAMP is not None:
        return _VERSION_STAMP
    try:
        from importlib.metadata import version

        ver = version("cagents")
    except Exception:
        ver = "?"
    commit = ""
    try:
        import subprocess

        repo = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            commit = "@" + out.stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, timeout=2,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                commit += "+dirty"
    except Exception:
        pass
    _VERSION_STAMP = f"cagents {ver} {commit}".strip()
    return _VERSION_STAMP


def _log(message: str) -> None:
    """Append-only trace of everything cagents-ctx does. These processes
    are invisible tmux hooks — when one misbehaves, this file is the only
    place the story exists. Best-effort: logging must never break the
    hook it's narrating."""
    global _LOG_FH
    if _LOG_FILE is None:
        return
    try:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if _LOG_FH is None:
            _LOG_FH = _LOG_FILE.open("a", encoding="utf-8")
        _LOG_FH.write(f"{stamp} [{os.getpid()}] {message}\n")
        _LOG_FH.flush()
    except (OSError, ValueError):
        _LOG_FH = None


def _display(message: str) -> None:
    _log(f"display: {message}")
    _tmux("display-message", message)


TOAST_REQUEST_FILE = "toast-request"


def queue_toast(state_dir: Path | None, message: str, severity: str = "warning") -> None:
    """Hand a message to the cagents app to show as a real toast — this
    process is a short-lived tmux hook, so a tmux display-message flash is
    all it can render itself (easy to miss). The app drains the file on
    its next refresh. Best-effort, append-only (two hooks racing just
    produce two lines)."""
    if state_dir is None:
        return
    _log(f"toast[{severity}]: {message}")
    try:
        with (state_dir / TOAST_REQUEST_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"message": message, "severity": severity}) + "\n")
    except OSError:
        pass


def shim_path_env(shim_dir: str) -> list[str]:
    """-e args putting the cagents `claude` shim first in shells: PATH for
    plain shells, ZDOTDIR (sibling of the shim dir) so zsh overrides any
    `claude` alias from the user's rc."""
    import os

    if not shim_dir:
        return []
    zdot = str(Path(shim_dir).parent / "zdot")
    return [
        "-e", f"PATH={shim_dir}:{os.environ.get('PATH', '')}",
        "-e", f"ZDOTDIR={zdot}",
    ]


def resolve_terminal_directory(directory: str) -> tuple[str, str, str]:
    """Where a terminal for `directory` should actually open.

    Claude Code's own EnterWorktree/ExitWorktree tools change its process
    cwd; every transcript record after that carries the new cwd (see
    claude_data.py's `last_cwd` / SessionView.work_dir), which is exactly
    what `directory` is here. That tells us where Claude is working NOW,
    but not whether it's a dedicated git worktree or the shared repo
    checkout — `gitops.worktree_status` resolves that with git itself.

    Returns (effective_dir, kind, warning):
      - ("linked", dir, "")            a real, dedicated worktree.
      - ("main", repo_root, "warning") no dedicated worktree — this
        session shares the plain repo checkout with anyone else working
        in it; still usable, but worth flagging loudly.
      - ("", "", "")                   nothing usable: not a real
        directory, or not a git working tree at all.
    """
    from . import gitops

    if not directory or not Path(directory).is_dir():
        return "", "", ""
    kind, root = gitops.worktree_status(directory)
    if kind == "linked":
        return directory, "linked", ""
    if kind == "main":
        effective = root or directory
        return effective, "main", (
            f"cagents: no dedicated worktree for this session — terminal opened "
            f"in the shared repo checkout ({effective})"
        )
    return "", "", ""


def _error_placeholder(message: str) -> list[str]:
    inner = f'printf "\\n  %s\\n" {shlex.quote("cagents: " + message)}; exec sleep 2147483647'
    return ["sh", "-c", inner]


def _last_term_target() -> str:
    """The command last respawned into the term-1 pane — a marker kept in
    the work server's own global env (not this short-lived process), so
    repeat clicks/keypresses on the SAME session's terminal don't kill a
    shell that's already there, whether they arrive via a mouse click on
    the tab or the "N" key."""
    out = _work_output("show-environment", "-g", "CAGENTS_TERM_TARGET")
    if "=" not in out:
        return ""
    return out.strip().split("=", 1)[1]


def _set_last_term_target(value: str) -> None:
    _work("set-environment", "-g", "CAGENTS_TERM_TARGET", value)


def do_shell(
    directory: str, session_id: str = "", tmux_name: str = "", tmux_socket: str = "",
    shim_dir: str = "", select: bool = True, state_dir: Path | None = None,
) -> int:
    """Point the terminal tab (or, outside tab mode, a fresh split) at
    THIS session's own worktree and its own persistent terminal window —
    never a shell shared with whichever session opened one last. Same
    entry point whether it's reached by clicking the tab (the
    after-select-window hook, --no-select) or by the global C-t / "N"
    key (select=True)."""
    if select and _workspace_alive():
        # C-t is a TOGGLE: pressed while a terminal tab is showing, it takes
        # you back to the session tab instead of re-opening the terminal.
        current = _work_output(
            "display-message", "-p", "-t", "=work:", "#{window_name}"
        ).strip()
        if current.startswith("term"):
            _log(f"do_shell: toggle back from {current!r} -> session tab")
            _work("select-window", "-t", "=work:session")
            _tmux("select-pane", "-t", ":.1")
            return 0
    effective_dir, kind, warning = resolve_terminal_directory(directory)
    _log(
        f"do_shell: dir={directory!r} -> effective={effective_dir!r} kind={kind!r} "
        f"session={session_id[:8]!r} tmux={tmux_name!r}@{tmux_socket!r} select={select}"
    )
    if not _workspace_alive():
        _log("do_shell: workspace not alive -> split-window fallback")
        if kind == "":
            return 1
        return _tmux("split-window", "-v", "-l", "12", *shim_path_env(shim_dir),
                     "-c", effective_dir)
    if kind == "":
        # Deliberately quiet (no toast/status flash — user choice): the term
        # tab itself shows the explanation via the placeholder.
        _work("respawn-pane", "-k", "-t", "=work:term-1",
              *_error_placeholder("no git worktree found for this session"))
        _set_last_term_target("")
        if select:
            _work("select-window", "-t", "=work:term-1")
            _tmux("select-pane", "-t", ":.1")
        return 1
    # Shared repo checkout (no dedicated worktree): open there, silently —
    # the warning toast/flash proved to be pure noise in practice.
    if tmux_name:
        from .tmuxctl import CREATE_SOCKET, TmuxClient

        socket = tmux_socket or CREATE_SOCKET
        client = TmuxClient()
        try:
            client.ensure_session_window(tmux_name, "term", effective_dir, socket=socket)
            group = client.ensure_window_view(
                tmux_name, "term", socket=socket, force_select=select
            )
        except RuntimeError as error:
            import traceback

            _log("do_shell: terminal setup failed:\n" + traceback.format_exc())
            _display(f"cagents: terminal setup failed: {error}")
            queue_toast(state_dir, f"Terminal setup failed: {error}", "error")
            return 1
        command = f"env -u TMUX tmux -L {socket} attach-session -t '={group}'"
    else:
        # No live claude session to scope the terminal to — a plain
        # shell directly in the resolved directory.
        env_prefix = ""
        if shim_dir:
            zdot = str(Path(shim_dir).parent / "zdot")
            env_prefix = f"PATH={shlex.quote(shim_dir)}:$PATH ZDOTDIR={shlex.quote(zdot)} "
        command = "sh -c " + shlex.quote(
            f"cd {shlex.quote(effective_dir)} && exec {env_prefix}${{SHELL:-sh}}"
        )
    _log(f"do_shell: target command = {command!r}")
    if command != _last_term_target():
        if "term-1" not in _work_windows():
            _work("new-window", "-d", "-t", "=work:", "-n", "term-1", command)
        else:
            _work("respawn-pane", "-k", "-t", "=work:term-1", command)
        _set_last_term_target(command)
    if select:
        _work("select-window", "-t", "=work:term-1")
        _tmux("select-pane", "-t", ":.1")  # focus the workspace pane
    return 0


# Piped through delta when installed (syntax highlighting, line numbers —
# genuinely legible instead of bare +/- red-green) falling back to the
# plain colored diff untouched otherwise. Only the actual `git diff`
# output goes through it — the header/status lines above it are plain
# text delta has no business reformatting.
_DELTA_OR_CAT = "(command -v delta >/dev/null 2>&1 && delta --line-numbers --paging=never || cat)"


def diff_popup_command(directory: str, mode: str = "branch") -> str:
    """One shell pipeline through a pager (q closes).

    mode "branch" (the default, and the important one): this worktree —
    committed AND uncommitted — versus master. The base is the merge-base
    with the first of: origin/HEAD, origin/main, origin/master, main,
    master. Remote-tracking refs first, because a linked worktree often has
    no (or a stale) local main.

    mode "uncommitted": only what isn't committed yet (vs HEAD).
    """
    q = shlex.quote(directory)
    if mode == "uncommitted":
        return (
            f"cd {q} && "
            '{ echo "# ${PWD##*/} $(git branch --show-current) — uncommitted changes"; '
            "git status --short; echo; "
            f'git diff --color=always HEAD | {_DELTA_OR_CAT}; }} | less -R'
        )
    return (
        f"cd {q} && "
        "ref=''; for cand in "
        '"$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" '
        "origin/main origin/master main master; do "
        '[ -n "$cand" ] && git rev-parse --verify --quiet "$cand" >/dev/null 2>&1 '
        '&& ref=$cand && break; done; '
        'base=""; [ -n "$ref" ] && base=$(git merge-base "$ref" HEAD 2>/dev/null); '
        'if [ "$(git rev-parse HEAD 2>/dev/null)" = "$base" ] && '
        '[ -z "$(git status --porcelain)" ]; then :; fi; '
        'if [ -n "$base" ] && [ "$(git rev-parse HEAD)" != "$base" ]; then vs=$ref; '
        'elif [ -n "$base" ]; then base=""; vs=uncommitted; else vs=uncommitted; fi; '
        '{ echo "# ${PWD##*/} $(git branch --show-current) vs $vs"; '
        "git status --short; echo; "
        f'git diff --color=always ${{base:+"$base"}} | {_DELTA_OR_CAT}; }} | less -R'
    )


LAZYGIT_CONFIG_DIR = Path.home() / ".local" / "share" / "cagents"


def _lazygit_config_path() -> Path:
    """disableStartupPopups: true — the automated diffing-mode keystrokes
    in _enter_lazygit_diffing_mode assume no onboarding popup is stealing
    them (confirmed live: without this, lazygit's one-time "thanks for
    using lazygit" popup eats the first keystroke)."""
    path = LAZYGIT_CONFIG_DIR / "lazygit.yml"
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("disableStartupPopups: true\n", "utf-8")
        except OSError:
            pass
    return path


def _merge_base_ref(directory: str) -> str:
    """The merge-base with the repo's default branch — not the branch tip
    itself: if main has moved on independently, diffing straight against
    its tip would show main's own commits as if they were being
    "reverted", which merge-base avoids. '' if there's no resolvable
    default branch."""
    from . import gitops

    branch = gitops.default_branch(directory)
    if not branch:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", directory, "merge-base", branch, "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def lazygit_command(directory: str) -> str:
    q = shlex.quote(directory)
    config = shlex.quote(str(_lazygit_config_path()))
    return f"cd {q} && lazygit --use-config-file={config}"


def _enter_lazygit_diffing_mode(window: str, ref: str) -> None:
    """Confirmed live: lazygit's diffing mode (W -> "Enter ref to diff" ->
    type a ref -> enter) accepts an arbitrary ref/SHA as free text and
    immediately shows the Files panel diffed against it, uncommitted
    changes included — a full "branch vs merge-base" view with zero
    manual steps. Needs a beat after launch for lazygit to finish its own
    startup render before it can receive keys; each step gets its own
    short pause rather than one long one, to keep each keystroke landing
    on the screen it was scripted for."""
    time.sleep(1.2)
    _work("send-keys", "-t", f"=work:{window}", "W")
    time.sleep(0.4)
    _work("send-keys", "-t", f"=work:{window}", "Enter")
    time.sleep(0.4)
    _work("send-keys", "-t", f"=work:{window}", "-l", ref)
    time.sleep(0.2)
    _work("send-keys", "-t", f"=work:{window}", "Enter")


def do_diff(directory: str, select: bool = True, mode: str = "branch") -> int:
    if not directory or not Path(directory).is_dir():
        _display("cagents: no directory for the selected session")
        return 1
    inside = subprocess.run(
        ["git", "-C", directory, "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=10,
    )
    if inside.returncode != 0:
        _display("cagents: not a git repository")
        return 1
    # Real mouse-interactive file-list + diff-panel app when available
    # (click files, scroll with the mouse, stage/unstage) — falling back
    # to the plain pager pipeline untouched otherwise, same as delta.
    use_lazygit = shutil.which("lazygit") is not None
    if _workspace_alive():
        # Tab mode: fresh diff in the diff tab; switch to it unless this run
        # IS the tab-click hook (which is already there).
        if not _recently_built():
            if use_lazygit:
                command = lazygit_command(directory)
            else:
                command = "sh -c " + shlex.quote(diff_popup_command(directory, mode))
            if "diff" not in _work_windows():
                _work("new-window", "-d", "-t", "=work:", "-n", "diff", command)
            else:
                _work("respawn-pane", "-k", "-t", "=work:diff", command)
            if use_lazygit and mode == "branch":
                ref = _merge_base_ref(directory)
                if ref:
                    _enter_lazygit_diffing_mode("diff", ref)
            _mark_built()
        if select:
            _work("select-window", "-t", "=work:diff")
            _tmux("select-pane", "-t", ":.1")
        return 0
    if use_lazygit:
        # A popup can't safely run the scripted-keystroke automation
        # (racy against the popup's own creation) — branch-mode diffing
        # here is a manual W + type-the-ref, same as plain lazygit usage.
        return _tmux(
            "display-popup", "-E", "-w", "92%", "-h", "88%",
            "sh", "-c", lazygit_command(directory),
        )
    return _tmux(
        "display-popup", "-E", "-w", "92%", "-h", "88%",
        "sh", "-c", diff_popup_command(directory, mode),
    )


def do_event(kind: str, path: Path) -> int:
    """Called by Claude Code hooks (Notification / Stop / UserPromptSubmit)
    on sessions cagents spawned. Merges {kind: now} into the session's
    events file; Notification also records its message and notification_type
    from the hook's stdin JSON. This is the authoritative 'what state is
    Claude in' signal that replaces pane heuristics for spawned sessions.

    notification_type matters: Claude Code fires this same hook for a real
    blocking dialog (permission_prompt, elicitation_dialog,
    elicitation_url_dialog, agent_needs_input) AND for a plain idle nudge
    (idle_prompt) once Claude's been waiting ~60s with nothing to do — the
    message text alone ("Claude is waiting for your input") is identical
    either way, so the type field is the only reliable way to tell them
    apart (see sessions.BLOCKING_NOTIFICATION_TYPES)."""
    import time

    events = read_context(path)  # same tolerant reader: dict or {}
    events[kind] = time.time()
    if kind == "Notification":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            message = str(payload.get("message", "")).strip()
            if message:
                events["message"] = message[:200]
            notification_type = str(payload.get("notification_type", "")).strip()
            if notification_type:
                events["notification_type"] = notification_type
            else:
                events.pop("notification_type", None)
        except (json.JSONDecodeError, OSError):
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events), "utf-8")
    except OSError:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cagents-ctx")
    parser.add_argument(
        "command", choices=["shell", "diff", "event", "wlog"],
        nargs="?", default="shell",
    )
    parser.add_argument("kind", nargs="?", default="")
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--context", type=Path, required=False)
    parser.add_argument("--no-select", action="store_true",
                        help="rebuild without switching tabs (used by the tab-click hook)")
    args = parser.parse_args(argv)

    if args.command == "wlog":
        # tmux-hook logging: quoting-proof (tmux expands #{formats} into the
        # kind argument; no shell/date/quote gymnastics involved).
        if args.context is None:
            return 2
        init_log(args.context.parent)
        _log(f"[tmux-hook] {args.kind}")
        return 0
    if args.command == "event":
        if not args.kind or args.file is None:
            return 2
        return do_event(args.kind, args.file)
    if args.context is None:
        return 2
    init_log(args.context.parent)
    context = read_context(args.context)
    _log(
        f"invoked ({version_stamp()}): "
        f"{argv if argv is not None else sys.argv[1:]} context={context}"
    )
    directory = str(context.get("dir", ""))
    if args.command == "shell":
        return do_shell(
            directory,
            session_id=str(context.get("session_id", "")),
            tmux_name=str(context.get("tmux_name", "")),
            tmux_socket=str(context.get("tmux_socket", "")),
            shim_dir=str(context.get("shim_dir", "")),
            select=not args.no_select,
            state_dir=args.context.parent,
        )
    return do_diff(
        directory, select=not args.no_select,
        mode=str(context.get("diff_mode", "branch")),
    )


def tmux_entry(argv: list[str] | None = None) -> int:
    """Entry point for tmux run-shell hooks. ALWAYS exits 0: a nonzero
    exit makes tmux paint a raw \'command returned N\' screen over the
    user's pane — pure noise, since every failure is already surfaced as
    an app toast and recorded in ctx.log. Crashes are logged, never shown."""
    try:
        code = main(argv)
        _log(f"exit: {code}")
    except SystemExit as error:  # argparse
        _log(f"exit: SystemExit {error.code}")
    except Exception:
        import traceback

        _log("crash:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(tmux_entry())
