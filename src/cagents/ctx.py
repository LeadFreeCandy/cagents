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


def _display(message: str) -> None:
    _tmux("display-message", message)


def do_shell(directory: str) -> int:
    if not directory or not Path(directory).is_dir():
        _display("cagents: no directory for the selected session")
        return 1
    return _tmux("split-window", "-v", "-l", "12", "-c", directory)


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


def do_diff(directory: str) -> int:
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
    return _tmux(
        "display-popup", "-E", "-w", "92%", "-h", "88%",
        "sh", "-c", diff_popup_command(directory),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cagents-ctx")
    parser.add_argument("command", choices=["shell", "diff"])
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args(argv)

    directory = str(read_context(args.context).get("dir", ""))
    if args.command == "shell":
        return do_shell(directory)
    return do_diff(directory)


if __name__ == "__main__":
    sys.exit(main())
