"""Sidecar mode: cagents as a persistent left rail.

When cagents runs inside a tmux container (the `cagents-shell` script,
which sets CAGENTS_SIDECAR=1), attaching stops meaning "hand over the
whole terminal". Instead the session opens in a pane to the right — a
nested attach onto the user's existing `claude` socket — and cagents
stays alive on the left, collapsed to a slim rail, states still ticking.

Focus movement and rail width are the container's job (tmux hooks in
cagents-shell); this module only opens/replaces the right pane. All tmux
calls here talk to the *outer* server via $TMUX, never the claude socket.
"""

from __future__ import annotations

import os
import subprocess

COLLAPSED_WIDTH = 34


class Sidecar:
    def __init__(self, runner=None, own_pane: str = ""):
        # runner: (args: list[str]) -> str, injectable for tests.
        self._run = runner or _outer_tmux
        self.own_pane = own_pane or os.environ.get("TMUX_PANE", "")
        self.pane_id: str = ""  # the session pane we manage on the right

    @staticmethod
    def enabled() -> bool:
        return bool(os.environ.get("TMUX")) and os.environ.get("CAGENTS_SIDECAR") == "1"

    def open(self, shell_command: str) -> None:
        """Run `shell_command` in the right pane (creating or replacing it),
        collapse the rail, and move focus to the session."""
        if self.pane_id and self._pane_alive():
            self._run(["respawn-pane", "-k", "-t", self.pane_id, shell_command])
        else:
            out = self._run(
                ["split-window", "-h", "-d", "-P", "-F", "#{pane_id}",
                 "-t", self.own_pane, shell_command]
            )
            self.pane_id = out.strip()
        if self.own_pane:
            self._run(["resize-pane", "-t", self.own_pane, "-x", str(COLLAPSED_WIDTH)])
        self._run(["select-pane", "-t", self.pane_id])

    def _pane_alive(self) -> bool:
        try:
            out = self._run(["list-panes", "-F", "#{pane_id}"])
        except RuntimeError:
            return False
        return self.pane_id in out.split()


def _outer_tmux(args: list[str]) -> str:
    """Talk to the enclosing tmux server (resolved from $TMUX)."""
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"tmux {args[0]} failed")
    return proc.stdout


def nested_attach_command(socket: str, session_name: str) -> str:
    """The command the right pane runs: attach to the claude-socket session,
    with $TMUX cleared so tmux allows the (deliberate) nesting."""
    safe = session_name.replace("'", "'\\''")
    return f"env -u TMUX tmux -L {socket} attach-session -t '={safe}'"
