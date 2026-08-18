"""CLI entry point: `cagents` or `python -m cagents`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cagents",
        description="A lightweight terminal supervisor for Claude Code sessions.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--claude-dir",
        type=Path,
        default=None,
        help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="cagents state file (default: ~/.local/share/cagents/state.json)",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="classic mode: attaching takes over the whole terminal "
        "(default is the sidecar container: list stays as a left rail)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="wipe cagents' own bookkeeping (tracked sessions, settings) and exit; "
        "Claude's transcripts are untouched",
    )
    parser.add_argument(
        "--preview-session",
        metavar="SESSION_ID",
        default=None,
        help=argparse.SUPPRESS,  # internal: render a dead session's transcript in a pane
    )
    args = parser.parse_args(argv)

    if args.version:
        from cagents import __version__

        print(f"cagents {__version__}")
        return 0

    from cagents.store import Store

    if args.reset:
        store = Store.load(args.store)
        count = len(store.sessions)
        answer = input(
            f"Wipe cagents' bookkeeping ({count} tracked session(s), settings)? "
            "Claude's transcripts are NOT touched. [y/N] "
        )
        if answer.strip().lower() == "y":
            store.reset()
            print("Reset. Claude's own session data is untouched.")
        else:
            print("Aborted.")
        return 0

    if args.preview_session:
        return _render_preview(args.preview_session, args.store, args.claude_dir)

    # Remember where the user actually launched from — new sessions default
    # here, and the container re-exec must not lose it.
    os.environ.setdefault("CAGENTS_LAUNCH_CWD", os.getcwd())

    from cagents.sidecar import bootstrap_container, should_bootstrap

    store = Store.load(args.store)
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if store.get_setting("sidebar") and should_bootstrap(
        os.environ, sys.stdout.isatty(), args.fullscreen
    ):
        bootstrap_container(raw_args)  # execs tmux attach; never returns

    if args.fullscreen:
        os.environ["CAGENTS_SIDECAR"] = "0"  # opt out even inside tmux

    from cagents.app import CagentsApp

    app = CagentsApp(store=store, claude_dir=args.claude_dir)
    app.run()
    return 0


def _render_preview(session_id: str, store_path: Path | None, claude_dir: Path | None) -> int:
    """Internal viewer mode: print a dead session's transcript (real parse,
    ANSI colors) into the pane, then hold so the pane stays readable.
    Scrollback works via the outer tmux's copy-mode/mouse wheel."""
    import threading

    from rich.console import Console

    from cagents.claude_data import default_claude_dir, parse_session_file
    from cagents.format import preview_renderable
    from cagents.sessions import SessionRegistry, SessionState, SessionView
    from cagents.store import Store, TrackedSession

    store = Store.load(store_path)
    tracked = store.sessions.get(session_id) or TrackedSession(
        session_id=session_id, project_dir="", added_at=""
    )
    registry = SessionRegistry(store, claude_dir=claude_dir or default_claude_dir())
    path = registry._find_session_file(tracked)
    console = Console(force_terminal=True)
    if path is None:
        console.print("[dim red]Transcript not found in Claude's store.[/dim red]")
    else:
        parsed = parse_session_file(path, tail_bytes=2 * 1024 * 1024, preview_items=300)
        view = SessionView(
            session_id=session_id,
            tracked=tracked,
            parsed=parsed,
            state=SessionState.STOPPED,
            live=False,
            state_detail="not running — enter to resume",
        )
        console.print(preview_renderable(view, width=console.width))
    try:
        threading.Event().wait()  # hold the pane; respawn/kill ends us
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
