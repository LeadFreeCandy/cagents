"""CLI entry point: `cagents` or `python -m cagents`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    direct = os.environ.get("CAGENTS_DIRECT") == "1"
    parser = argparse.ArgumentParser(
        prog="cagents3" if direct else "cagents",
        description="A lightweight terminal supervisor for Claude Code and Codex sessions.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--claude-dir",
        type=Path,
        default=None,
        help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    parser.add_argument(
        "--codex-dir", type=Path, default=None,
        help="Codex data dir (default: $CODEX_HOME or ~/.codex)",
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
        "agent transcripts are untouched",
    )
    args = parser.parse_args(argv)

    if args.version:
        from cagents import __version__

        print(f"cagents {__version__}")
        return 0

    from cagents.store import Store

    if direct and args.store is None:
        from cagents.direct_cli import seed_state, state_path
        args.store = state_path()
        if not args.reset:
            seed_state(args.store)

    if args.reset:
        store = Store.load(args.store)
        count = len(store.sessions)
        answer = input(
            f"Wipe cagents' bookkeeping ({count} tracked session(s), settings)? "
            "Agent transcripts are NOT touched. [y/N] "
        )
        if answer.strip().lower() == "y":
            store.reset()
            print("Reset. Agent session data is untouched.")
        else:
            print("Aborted.")
        return 0

    # Remember where the user actually launched from — new sessions default
    # here, and the container re-exec must not lose it.
    os.environ.setdefault("CAGENTS_LAUNCH_CWD", os.getcwd())
    # Remember the real terminal app before tmux overwrites TERM_PROGRAM with
    # "tmux" inside the container — the notifier needs it for branding and
    # click-to-activate.
    term = os.environ.get("TERM_PROGRAM", "")
    if term and term != "tmux":
        os.environ.setdefault("CAGENTS_TERM_PROGRAM", term)

    from cagents.sidecar import bootstrap_container, should_bootstrap

    store = Store.load(args.store)
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if direct:
        from cagents.direct_cli import bootstrap, needs_bootstrap
        if needs_bootstrap(os.environ, sys.stdout.isatty()):
            bootstrap(raw_args)
    elif not direct and store.get_setting("sidebar") and should_bootstrap(
        os.environ, sys.stdout.isatty(), args.fullscreen
    ):
        bootstrap_container(raw_args)  # execs tmux attach; never returns

    if args.fullscreen and not direct:
        os.environ["CAGENTS_SIDECAR"] = "0"  # opt out even inside tmux

    from cagents.app import CagentsApp

    if direct:
        from cagents.direct_app import DirectApp as CagentsApp

    app = CagentsApp(store=store, claude_dir=args.claude_dir, codex_dir=args.codex_dir)
    app.run()
    if app.restart_requested:
        os.execv(sys.executable, [sys.executable, "-m", "cagents", *raw_args])
    return 0


if __name__ == "__main__":
    sys.exit(main())
