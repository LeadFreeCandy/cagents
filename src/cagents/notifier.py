"""Desktop notifications (macOS) for sessions that start needing you.

With terminal-notifier installed, the notification is branded as the
terminal app hosting cagents (its icon and name, via terminal-notifier's
-sender) instead of showing up as "Script Editor" — which is genuinely
unavoidable with plain osascript: `display notification` has no sender
override at all, it's always attributed to whatever runs the script.
Clicking the notification both (a) activates that same terminal app — via
-activate — and (b) writes the session id to a small request file;
cagents polls it each refresh and selects that task in the list. Both use
the bundle id read straight off $TERM_PROGRAM at notify time, since
notify_desktop always runs inside the same process that inherited the
launching terminal's environment. Without terminal-notifier we fall back
to osascript's display notification (no branding, no click action —
macOS gives scripts no way to observe the click either).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SELECT_REQUEST_FILE = "select-request"

# $TERM_PROGRAM -> the app's bundle id, for terminal-notifier's -activate.
# Unrecognized/unset values just skip activation (falls back to today's
# "select only if you're already looking at it" behavior).
_TERM_PROGRAM_BUNDLE_IDS = {
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
    "ghostty": "com.mitchellh.ghostty",
    "WezTerm": "com.github.wez.wezterm",
    "vscode": "com.microsoft.VSCode",
    "Hyper": "co.zeit.hyper",
    "Tabby": "org.tabby",
    "Warp": "dev.warp.Warp-Stable",
}


def _terminal_bundle_id() -> str | None:
    return _TERM_PROGRAM_BUNDLE_IDS.get(os.environ.get("TERM_PROGRAM", ""))


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
            args = [
                tn,
                "-title", title,
                "-message", message,
                "-group", f"cagents-{session_id[:8]}",
                "-execute", f"/bin/sh -c \"echo {session_id} > '{request}'\"",
            ]
            bundle_id = _terminal_bundle_id()
            if bundle_id:
                args += ["-activate", bundle_id, "-sender", bundle_id]
            subprocess.run(args, capture_output=True, timeout=10)
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
