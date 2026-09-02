"""Every tmux socket cagents talks to, named in one place.

CAGENTS_SOCKET_SUFFIX moves the whole installation onto its own set of
servers -- container, workspace, spawned sessions, and the `claude`
socket it discovers on -- so a second cagents can run beside the real one
without either seeing the other's sessions. It exists for testing the
actual app against a real Claude Code session; cagents never sets it.
Pair it with `--store` for a separate state file (the shim directory
follows the store, so that isolates the bookkeeping too):

    CAGENTS_SOCKET_SUFFIX=-probe cagents --store /tmp/probe/state.json

Do NOT isolate a test instance by moving XDG_DATA_HOME instead: Claude
Code keeps its installed versions under there and repoints
~/.local/bin/claude at whatever it finds, so a throwaway data dir takes
the real `claude` command down with it when you delete it.
"""

from __future__ import annotations

import os


def socket_name(base: str) -> str:
    return base + os.environ.get("CAGENTS_SOCKET_SUFFIX", "")
