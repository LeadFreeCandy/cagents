"""cagents' own persistent state — deliberately minimal (spec §9).

Only things Claude Code itself has no way to represent are stored here:
which sessions the user brought into cagents, whether a human has reviewed
a finished session (and when), an optional label and note. Everything else
is derived live from Claude's own store.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STORE_VERSION = 1


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
    note: str = ""
    reviewed_at: str = ""  # ISO 8601, empty = never reviewed

    def reviewed_datetime(self) -> datetime | None:
        if not self.reviewed_at:
            return None
        try:
            return datetime.fromisoformat(self.reviewed_at)
        except ValueError:
            return None

    def to_dict(self) -> dict:
        return {
            "project_dir": self.project_dir,
            "added_at": self.added_at,
            "label": self.label,
            "note": self.note,
            "reviewed_at": self.reviewed_at,
        }

    @classmethod
    def from_dict(cls, session_id: str, data: dict) -> "TrackedSession":
        return cls(
            session_id=session_id,
            project_dir=str(data.get("project_dir", "")),
            added_at=str(data.get("added_at", "")),
            label=str(data.get("label", "")),
            note=str(data.get("note", "")),
            reviewed_at=str(data.get("reviewed_at", "")),
        )


@dataclass
class Store:
    path: Path
    sessions: dict[str, TrackedSession] = field(default_factory=dict)

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
        return store

    def save(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "sessions": {sid: t.to_dict() for sid, t in self.sessions.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
        os.replace(tmp, self.path)

    # -- mutations (each saves immediately; the store is tiny) --------------

    def track(self, session_id: str, project_dir: str, added_at: str, label: str = "") -> TrackedSession:
        tracked = self.sessions.get(session_id)
        if tracked is None:
            tracked = TrackedSession(
                session_id=session_id,
                project_dir=project_dir,
                added_at=added_at,
                label=label,
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

    def set_note(self, session_id: str, note: str) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.note = note
            self.save()

    def set_label(self, session_id: str, label: str) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.label = label
            self.save()
