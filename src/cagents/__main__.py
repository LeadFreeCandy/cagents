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

    import logging
    import os

    from cagents.logging_setup import configure_logging
    from cagents.sidecar import bootstrap_container, should_bootstrap
    from cagents.store import Store

    log_path = configure_logging()
    logger = logging.getLogger("cagents.main")
    logger.info("cagents starting: argv=%r pid=%s", argv or sys.argv[1:], os.getpid())

    store = Store.load(args.store)
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    sidebar_setting = store.get_setting("sidebar")
    will_bootstrap = should_bootstrap(os.environ, sys.stdout.isatty(), args.fullscreen)
    logger.info(
        "startup mode: sidebar_setting=%s TMUX=%r CAGENTS_SIDECAR=%r isatty=%s "
        "fullscreen_flag=%s -> bootstrap_container=%s",
        sidebar_setting, os.environ.get("TMUX"), os.environ.get("CAGENTS_SIDECAR"),
        sys.stdout.isatty(), args.fullscreen, sidebar_setting and will_bootstrap,
    )
    if sidebar_setting and will_bootstrap:
        logger.info("bootstrapping the sidecar container (execvp — this process is replaced)")
        bootstrap_container(raw_args)  # execs tmux attach; never returns

    if args.fullscreen:
        os.environ["CAGENTS_SIDECAR"] = "0"  # opt out even inside tmux

    from cagents.app import CagentsApp

    logger.info("running CagentsApp in-process (log: %s)", log_path)
    app = CagentsApp(store=store, claude_dir=args.claude_dir)
    try:
        app.run()
    except Exception:
        logger.exception("CagentsApp crashed")
        raise
    logger.info("cagents exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
