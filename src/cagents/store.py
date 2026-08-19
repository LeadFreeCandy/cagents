"""cagents' own persistent state — deliberately minimal (spec §9).

Only things Claude Code itself has no way to represent are stored here:
which sessions the user brought into cagents, whether a human has accepted
a finished session (done), whether it's parked on the outside world
(waiting-external, tied to a PR), an optional label/note, and lineage.
Everything else is derived live from Claude's own store.

Every human-state field is a timestamp compared against the transcript's
last activity, never a flag — so new work by Claude automatically
invalidates stale human judgments without any syncing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STORE_VERSION = 1


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# User-facing toggles (the `,` settings panel). Everything defaults here;
# only overrides are persisted.
SETTINGS_DEFAULTS: dict[str, object] = {
    # Attach opens sessions in a side pane, keeping the list as a left rail.
    "sidebar": True,
    # Toast notifications (bottom-right). Errors always show regardless.
    "notifications": False,
    # Left arrow drives the layout cycle / returns to the list.
    "capture_left": True,
    # macOS notification when a session starts needing you; clicking it
    # selects the task in the list.
    "desktop_notifications": False,
    # What the diff tab shows. "branch": this worktree vs master
    # (merge-base, committed + uncommitted). "uncommitted": vs HEAD only.
    "diff_mode": "branch",
    # Queue/list priority of the states, most-urgent first. Reorder in the
    # settings panel's Priority tab.
    "state_order": [
        "needs input", "needs review", "monitoring", "background",
        "working", "waiting", "stopped", "done",
    ],
}


def default_store_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "cagents" / "state.json"


@dataclass
class TrackedSession:
    session_id: str
    project_dir: str  # the real cwd the session runs in
    added_at: str  # ISO 8601
    label: str = ""
    reviewed_at: str = ""  # ISO 8601, empty = not accepted ("done")
    archived: bool = False  # hidden from session views; history kept
    # Parked on the outside world (PR review). Cleared by new local
    # activity; resolved by the PR poller (comments -> re-alert, merge -> done).
    waiting_since: str = ""  # ISO 8601
    waiting_pr: str = ""  # PR url the wait is tied to
    external_update: str = ""  # last poller verdict: "github comments" | "merged"
    # Lineage (spec §9's "lightweight relationships").
    parent_id: str = ""
    relation: str = ""  # "fork" | "handoff" | ""

    def reviewed_datetime(self) -> datetime | None:
        return _parse_iso(self.reviewed_at)

    def waiting_datetime(self) -> datetime | None:
        return _parse_iso(self.waiting_since)

    def to_dict(self) -> dict:
        return {
            "project_dir": self.project_dir,
            "added_at": self.added_at,
            "label": self.label,
            "reviewed_at": self.reviewed_at,
            "archived": self.archived,
            "waiting_since": self.waiting_since,
            "waiting_pr": self.waiting_pr,
            "external_update": self.external_update,
            "parent_id": self.parent_id,
            "relation": self.relation,
        }

    @classmethod
    def from_dict(cls, session_id: str, data: dict) -> "TrackedSession":
        return cls(
            session_id=session_id,
            project_dir=str(data.get("project_dir", "")),
            added_at=str(data.get("added_at", "")),
            label=str(data.get("label", "")),
            reviewed_at=str(data.get("reviewed_at", "")),
            archived=bool(data.get("archived", False)),
            waiting_since=str(data.get("waiting_since", "")),
            waiting_pr=str(data.get("waiting_pr", "")),
            external_update=str(data.get("external_update", "")),
            parent_id=str(data.get("parent_id", "")),
            relation=str(data.get("relation", "")),
        )


@dataclass
class Store:
    path: Path
    sessions: dict[str, TrackedSession] = field(default_factory=dict)
    settings: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Store":
        path = path or default_store_path()
        store = cls(path=path)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return store
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            for sid, data in sessions.items():
                if isinstance(data, dict):
                    store.sessions[sid] = TrackedSession.from_dict(sid, data)
        settings = raw.get("settings")
        if isinstance(settings, dict):
            for key, value in settings.items():
                default = SETTINGS_DEFAULTS.get(key)
                if default is not None and isinstance(value, type(default)):
                    store.settings[key] = value
        return store

    def save(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "sessions": {sid: t.to_dict() for sid, t in self.sessions.items()},
            "settings": self.settings,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
        os.replace(tmp, self.path)

    def reset(self) -> None:
        """Wipe cagents' own bookkeeping. Claude's transcripts are untouched."""
        self.sessions.clear()
        self.settings.clear()
        self.save()

    # -- mutations (each saves immediately; the store is tiny) --------------

    def track(
        self,
        session_id: str,
        project_dir: str,
        added_at: str,
        label: str = "",
        parent_id: str = "",
        relation: str = "",
    ) -> TrackedSession:
        tracked = self.sessions.get(session_id)
        if tracked is None:
            tracked = TrackedSession(
                session_id=session_id,
                project_dir=project_dir,
                added_at=added_at,
                label=label,
                parent_id=parent_id,
                relation=relation,
            )
            self.sessions[session_id] = tracked
            self.save()
        return tracked

    def untrack(self, session_id: str) -> None:
        if self.sessions.pop(session_id, None) is not None:
            self.save()

    def mark_reviewed(self, session_id: str, when: str) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.reviewed_at = when
            self.save()

    def clear_reviewed(self, session_id: str) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None and tracked.reviewed_at:
            tracked.reviewed_at = ""
            self.save()

    def set_label(self, session_id: str, label: str) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.label = label
            self.save()

    def set_archived(self, session_id: str, archived: bool) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None and tracked.archived != archived:
            tracked.archived = archived
            self.save()

    # -- waiting on external (PR) -------------------------------------------

    def set_waiting(self, session_id: str, when: str, pr_url: str) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.waiting_since = when
            tracked.waiting_pr = pr_url
            tracked.external_update = ""
            self.save()

    def clear_waiting(self, session_id: str, external_update: str = "") -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.waiting_since = ""
            tracked.external_update = external_update
            self.save()

    # -- settings -------------------------------------------------------------

    def get_setting(self, key: str):
        return self.settings.get(key, SETTINGS_DEFAULTS.get(key, False))

    def set_setting(self, key: str, value) -> None:
        default = SETTINGS_DEFAULTS.get(key)
        if default is None or not isinstance(value, type(default)):
            return
        self.settings[key] = value
        self.save()
