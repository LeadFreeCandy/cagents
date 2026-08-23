"""`claude agents --json` — Claude Code's own first-class state API.

Documented (`claude agents --help`: "Print active sessions (interactive
and background) as a JSON array and exit (for scripting; does not require
a TTY)") and confirmed live: it reports, per *interactive* session,
`"status": "busy" | "idle" | "waiting"` — with a `"waitingFor"` field
("permission prompt") when waiting — keyed by the exact `sessionId`
cagents already tracks. Background sessions instead carry `"state":
"done" | "blocked"`.

This is authoritative and entirely independent of pane text or hooks, so
it sidesteps the whole class of bugs this project kept hitting by
scraping rendered terminal output: spinner format changes across Claude
Code versions, terminal-width reflow splitting a status line across two
physical lines, and a stale hook event outliving the moment it described
(a permission prompt's Notification event that never gets superseded once
you approve it via the dialog rather than typing a new message). Checked
as the first signal in `derive_state`, ahead of both the hooks-based
events file and pane heuristics — those remain as fallback for whatever
this doesn't cover (the `claude` binary missing/too old, or the command
failing for any reason).
"""

from __future__ import annotations

import json
import subprocess


def _default_runner(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=10.0)
    return proc.stdout


def fetch_agent_states(runner=None) -> dict[str, dict]:
    """{session_id: entry} for every session `claude agents --json --all`
    currently reports. Empty dict on any failure — never raises; this is
    a best-effort authoritative signal, not a required one."""
    run = runner or _default_runner
    try:
        out = run(["claude", "agents", "--json", "--all"])
    except Exception:
        return {}
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        entry["sessionId"]: entry
        for entry in data
        if isinstance(entry, dict) and entry.get("sessionId")
    }
