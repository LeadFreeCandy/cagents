"""Idle lifecycle policy. UI input and conversation activity have separate clocks."""

from __future__ import annotations

import re
from datetime import timezone

from .sessions import SessionState, SessionView
from .store import _parse_iso

SUSPEND_AFTER = 3600.0


def timestamp(value: str) -> float:
    parsed = _parse_iso(value)
    if parsed is None:
        return 0.0
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def duration_seconds(value: object) -> float:
    """Allow custom durations in state.json as well as the settings presets."""
    if value == "off":
        return 0.0
    match = re.fullmatch(r"(\d+)([mhdw])", str(value))
    if not match or int(match[1]) == 0:
        return 7 * 86400.0
    return int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]


def last_interaction(view: SessionView) -> float:
    """Old installs have no UI clock: use their existing activity/bookkeeping.

    Conversation records also reset idleness, including input from another UI.
    File mtimes are only a fallback; resuming can touch an unchanged transcript.
    """
    tracked = view.tracked
    return max(
        timestamp(tracked.last_interacted_at), timestamp(tracked.added_at),
        timestamp(tracked.reviewed_at), timestamp(tracked.waiting_since),
        timestamp(tracked.external_update_since),
        view.last_activity.timestamp() if view.last_activity else 0.0,
    )


def apply_auto_done(view: SessionView, duration: object, now: float) -> None:
    """Derive the annotation; the UI persists new transitions with its store writes."""
    view.auto_done = False
    reviewed = timestamp(view.tracked.reviewed_at)
    activity = view.last_activity.timestamp() if view.last_activity else 0.0
    if view.suspended and reviewed and reviewed >= activity:
        view.state, view.state_detail = SessionState.DONE, "done"
    view.done_at = timestamp(view.tracked.reviewed_at) if view.state == SessionState.DONE else 0.0
    if view.missing or view.state in (SessionState.DONE, SessionState.WORKING, SessionState.SNOOZED):
        return
    seconds = duration_seconds(duration)
    last = last_interaction(view)
    if not seconds or not last or now - last < seconds:
        return
    previous = timestamp(view.tracked.auto_done_at)
    view.auto_done = True
    view.done_at = previous if previous >= last else now
    view.state = SessionState.DONE
    view.state_detail = "Done (auto)"
    view.did_line = view.needs_line = ""


def should_suspend(view: SessionView, now: float) -> bool:
    return (
        view.state == SessionState.DONE and view.live and not view.suspended
        and not view.missing and bool(view.tmux_name)
        and last_interaction(view) > 0
        and now - last_interaction(view) >= SUSPEND_AFTER
    )
