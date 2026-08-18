"""Desktop notifications (macOS) for sessions that start needing you.

With terminal-notifier installed, clicking the notification writes the
session id to a small request file; cagents polls it each refresh and
selects that task in the list. Without terminal-notifier we fall back to
osascript's display notification (no click action — macOS gives scripts
no way to observe the click).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SELECT_REQUEST_FILE = "select-request"


def notify_desktop(
    title: str,
    message: str,
    session_id: str,
    state_dir: Path,
    tn_bin: str | None = None,
) -> None:
    """Fire-and-forget; failures are silent by design (a broken notifier
    must never take the app down — the in-app list is the ground truth)."""
    tn = tn_bin if tn_bin is not None else shutil.which("terminal-notifier")
    try:
        if tn:
            request = state_dir / SELECT_REQUEST_FILE
            subprocess.run(
                [
                    tn,
                    "-title", title,
                    "-message", message,
                    "-group", f"cagents-{session_id[:8]}",
                    "-execute", f"/bin/sh -c \"echo {session_id} > '{request}'\"",
                ],
                capture_output=True,
                timeout=10,
            )
        else:
            script = (
                f'display notification "{_esc(message)}" '
                f'with title "{_esc(title)}"'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')[:200]


def read_select_request(state_dir: Path) -> str | None:
    """The session id a clicked notification asked us to select, if any.
    Reading consumes the request."""
    request = state_dir / SELECT_REQUEST_FILE
    try:
        session_id = request.read_text("utf-8").strip()
    except OSError:
        return None
    try:
        request.unlink()
    except OSError:
        pass
    return session_id or None
