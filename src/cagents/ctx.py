"""cagents-ctx — tmux-side helper for the global C-s / C-d keys.

The app continuously writes the current session's directory to a small
context file; tmux root bindings invoke this script, which reads it and
acts *inside the same tmux server* (run-shell provides $TMUX):

    cagents-ctx shell --context <file>   split a terminal in that dir
    cagents-ctx diff  --context <file>   popup: worktree diff vs master

Kept dependency-free and instant — it runs on a keypress.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

CONTEXT_FILE = "context.json"
WORK_SOCKET = "cagents-work"  # the tabbed workspace (see sidecar.py)


def read_context(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_context(path: Path, directory: str, session_id: str) -> None:
    payload = json.dumps({"dir": directory, "session_id": session_id})
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


def _display(message: str) -> None:
    _tmux("display-message", message)


def do_shell(directory: str) -> int:
    if not directory or not Path(directory).is_dir():
        _display("cagents: no directory for the selected session")
        return 1
    if _workspace_alive():
        # Tab mode: the persistent terminal tab (recreate only if it died).
        if "term-1" not in _work_windows():
            _work("new-window", "-d", "-t", "=work:", "-n", "term-1", "-c", directory)
        _work("select-window", "-t", "=work:term-1")
        _tmux("select-pane", "-t", ":.1")  # focus the workspace pane
        return 0
    return _tmux("split-window", "-v", "-l", "12", "-c", directory)  # fullscreen fallback


def diff_popup_command(directory: str) -> str:
    """One shell pipeline: header, diff vs the default branch's merge-base
    (or just uncommitted changes on the default branch), untracked files
    listed — through a pager. q closes the popup."""
    q = shlex.quote(directory)
    return (
        f"cd {q} && "
        "base=$(git merge-base $(git symbolic-ref --quiet --short "
        "refs/remotes/origin/HEAD 2>/dev/null || echo main) HEAD 2>/dev/null || "
        "git merge-base master HEAD 2>/dev/null); "
        'if [ "$(git rev-parse HEAD 2>/dev/null)" = "$base" ]; then base=""; fi; '
        'if [ -n "$base" ]; then vs=$(git name-rev --name-only "$base"); '
        'else vs=uncommitted; fi; '
        '{ echo "# ${PWD##*/} $(git branch --show-current) vs $vs"; '
        "git status --short; echo; "
        'git diff --color ${base:+"$base"}; } | less -R'
    )


def do_diff(directory: str, select: bool = True) -> int:
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
    if _workspace_alive():
        # Tab mode: fresh diff in the diff tab; switch to it unless this run
        # IS the tab-click hook (which is already there).
        if not _recently_built():
            command = "sh -c " + shlex.quote(diff_popup_command(directory))
            if "diff" not in _work_windows():
                _work("new-window", "-d", "-t", "=work:", "-n", "diff", command)
            else:
                _work("respawn-pane", "-k", "-t", "=work:diff", command)
            _mark_built()
        if select:
            _work("select-window", "-t", "=work:diff")
            _tmux("select-pane", "-t", ":.1")
        return 0
    return _tmux(
        "display-popup", "-E", "-w", "92%", "-h", "88%",
        "sh", "-c", diff_popup_command(directory),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cagents-ctx")
    parser.add_argument("command", choices=["shell", "diff"])
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--no-select", action="store_true",
                        help="rebuild without switching tabs (used by the tab-click hook)")
    args = parser.parse_args(argv)

    directory = str(read_context(args.context).get("dir", ""))
    if args.command == "shell":
        return do_shell(directory)
    return do_diff(directory, select=not args.no_select)


if __name__ == "__main__":
    sys.exit(main())
