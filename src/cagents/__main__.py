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
    args = parser.parse_args(argv)

    if args.version:
        from cagents import __version__

        print(f"cagents {__version__}")
        return 0

    from cagents.app import CagentsApp
    from cagents.store import Store

    store = Store.load(args.store)
    app = CagentsApp(store=store, claude_dir=args.claude_dir)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
