"""CLI entry point: `cagents` or `python -m cagents`."""

from __future__ import annotations

import argparse
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
    args = parser.parse_args(argv)

    if args.version:
        from cagents import __version__

        print(f"cagents {__version__}")
        return 0

    import os

    from cagents.sidecar import bootstrap_container, should_bootstrap
    from cagents.store import Store

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


if __name__ == "__main__":
    sys.exit(main())
