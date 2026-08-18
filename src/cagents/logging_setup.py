"""File logging for cagents itself (never the sessions it supervises).

Off by default (library code, tests); `__main__` turns it on before anything
else runs, so the early bootstrap/sidecar decisions land in the file even if
the TUI never gets to draw a frame.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


def default_log_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "cagents" / "cagents.log"


def configure_logging(path: Path | None = None, level: int = logging.DEBUG) -> Path:
    """Attach a file handler to the "cagents" logger tree. Safe to call more
    than once (e.g. in tests) — repeat calls are no-ops after the first."""
    path = path or default_log_path()
    logger = logging.getLogger("cagents")
    if logger.handlers:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return path
