"""cagents' own persistent state — deliberately minimal (spec §9).

Only things Claude Code itself has no way to represent are stored here:
which sessions the user brought into cagents, whether a human has reviewed
a finished session (and when), an optional label and note. Everything else
is derived live from Claude's own store.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("cagents.store")

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
    # In the sidecar container: pressing Left inside a session closes its
    # pane (session keeps running) and returns to the list.
    "capture_left": True,
    # macOS notification when a session starts needing you; clicking it
    # selects the task in the list.
    "desktop_notifications": False,
    # Open todos with no activity for this many days pause themselves.
    # 0 disables.
    "auto_pause_days": 7,
    # Todo rows waiting on a human get did/needs sub-rows under them.
    "todo_status_lines": True,
    # The todos view/feature (view `4`, its footer key, `n` inside it,
    # worktrees). Off for anyone who doesn't use todos and would rather
    # not see it competing for room in the footer.
    "todos_enabled": True,
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
    note: str = ""
    reviewed_at: str = ""  # ISO 8601, empty = never reviewed
    archived: bool = False  # hidden from session views; history kept
    # "I've seen it, keep watching": quieter than needs-review until new
    # activity arrives (then it demands review again).
    monitoring_since: str = ""
    # Lineage (spec §9's "lightweight relationships"): where this session
    # came from, when cagents created it from another one.
    parent_id: str = ""
    relation: str = ""  # "fork" | "handoff" | ""

    def reviewed_datetime(self) -> datetime | None:
        return _parse_iso(self.reviewed_at)

    def monitoring_datetime(self) -> datetime | None:
        return _parse_iso(self.monitoring_since)

    def to_dict(self) -> dict:
        return {
            "project_dir": self.project_dir,
            "added_at": self.added_at,
            "label": self.label,
            "note": self.note,
            "reviewed_at": self.reviewed_at,
            "archived": self.archived,
            "monitoring_since": self.monitoring_since,
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
            note=str(data.get("note", "")),
            reviewed_at=str(data.get("reviewed_at", "")),
            archived=bool(data.get("archived", False)),
            monitoring_since=str(data.get("monitoring_since", "")),
            parent_id=str(data.get("parent_id", "")),
            relation=str(data.get("relation", "")),
        )


@dataclass
class Todo:
    """A unit of intent. Todos can spawn sessions (and worktrees); completing
    one is the natural moment to archive the workspaces it spawned."""

    todo_id: str
    text: str
    created_at: str  # ISO 8601
    done_at: str = ""  # ISO 8601, empty = open
    project_dir: str = ""  # default place its sessions start
    worktree: str = ""  # worktree created for this todo, if any
    session_ids: list[str] = field(default_factory=list)
    # Paused: shelved for now, with an optional way back.
    paused_at: str = ""  # ISO 8601, empty = not paused
    wake_at: str = ""  # ISO 8601 timer, if any
    wake_criteria: str = ""  # human description of the wake condition
    wake_script: str = ""  # path to the generated check script, if any

    @property
    def done(self) -> bool:
        return bool(self.done_at)

    @property
    def paused(self) -> bool:
        return bool(self.paused_at) and not self.done

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "created_at": self.created_at,
            "done_at": self.done_at,
            "project_dir": self.project_dir,
            "worktree": self.worktree,
            "session_ids": list(self.session_ids),
            "paused_at": self.paused_at,
            "wake_at": self.wake_at,
            "wake_criteria": self.wake_criteria,
            "wake_script": self.wake_script,
        }

    @classmethod
    def from_dict(cls, todo_id: str, data: dict) -> "Todo":
        raw_ids = data.get("session_ids", [])
        return cls(
            todo_id=todo_id,
            text=str(data.get("text", "")),
            created_at=str(data.get("created_at", "")),
            done_at=str(data.get("done_at", "")),
            project_dir=str(data.get("project_dir", "")),
            worktree=str(data.get("worktree", "")),
            session_ids=[str(s) for s in raw_ids] if isinstance(raw_ids, list) else [],
            paused_at=str(data.get("paused_at", "")),
            wake_at=str(data.get("wake_at", "")),
            wake_criteria=str(data.get("wake_criteria", "")),
            wake_script=str(data.get("wake_script", "")),
        )


@dataclass
class Store:
    path: Path
    sessions: dict[str, TrackedSession] = field(default_factory=dict)
    todos: dict[str, Todo] = field(default_factory=dict)
    settings: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Store":
        path = path or default_store_path()
        store = cls(path=path)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.info("store load: %s (%s) -> fresh store", path, error)
            return store
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            for sid, data in sessions.items():
                if isinstance(data, dict):
                    store.sessions[sid] = TrackedSession.from_dict(sid, data)
        todos = raw.get("todos")
        if isinstance(todos, dict):
            for tid, data in todos.items():
                if isinstance(data, dict):
                    store.todos[tid] = Todo.from_dict(tid, data)
        settings = raw.get("settings")
        if isinstance(settings, dict):
            for key, value in settings.items():
                default = SETTINGS_DEFAULTS.get(key)
                if default is None:
                    continue
                if isinstance(default, bool):
                    if isinstance(value, bool):
                        store.settings[key] = value
                elif isinstance(default, (int, float)) and isinstance(value, (int, float)):
                    store.settings[key] = value
        logger.info("store loaded: %s settings=%r", path, store.settings)
        return store

    def save(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "sessions": {sid: t.to_dict() for sid, t in self.sessions.items()},
            "todos": {tid: t.to_dict() for tid, t in self.todos.items()},
            "settings": self.settings,
        }
        if logger.isEnabledFor(logging.DEBUG):
            import traceback

            caller = "".join(traceback.format_stack(limit=4)[:-1])  # who called save(), 3 frames up
            logger.debug("save() settings=%r called from:\n%s", self.settings, caller)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
        os.replace(tmp, self.path)

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

    def set_archived(self, session_id: str, archived: bool) -> None:
        tracked = self.sessions.get(session_id)
        if tracked is not None and tracked.archived != archived:
            tracked.archived = archived
            self.save()

    # -- settings -------------------------------------------------------------

    def get_setting(self, key: str):
        return self.settings.get(key, SETTINGS_DEFAULTS.get(key, False))

    def set_setting(self, key: str, value) -> None:
        if key not in SETTINGS_DEFAULTS:
            return
        default = SETTINGS_DEFAULTS[key]
        if isinstance(default, bool) and not isinstance(value, bool):
            return
        if not isinstance(default, bool) and not isinstance(value, (int, float)):
            return
        logger.info("setting changed: %s = %r (was %r)", key, value, self.settings.get(key))
        self.settings[key] = value
        self.save()

    def set_monitoring(self, session_id: str, when: str) -> None:
        """when empty clears monitoring."""
        tracked = self.sessions.get(session_id)
        if tracked is not None:
            tracked.monitoring_since = when
            self.save()

    # -- pause / wake -----------------------------------------------------

    def pause_todo(self, todo_id: str, paused_at: str, wake_at: str = "",
                   wake_criteria: str = "", wake_script: str = "") -> None:
        todo = self.todos.get(todo_id)
        if todo is not None:
            todo.paused_at = paused_at
            todo.wake_at = wake_at
            todo.wake_criteria = wake_criteria
            todo.wake_script = wake_script
            self.save()

    def unpause_todo(self, todo_id: str) -> None:
        todo = self.todos.get(todo_id)
        if todo is not None and todo.paused_at:
            todo.paused_at = ""
            todo.wake_at = ""
            todo.wake_criteria = ""
            todo.wake_script = ""
            self.save()

    # -- todos ----------------------------------------------------------------

    def add_todo(self, text: str, created_at: str, project_dir: str = "") -> Todo:
        import uuid

        todo = Todo(
            todo_id=uuid.uuid4().hex[:12],
            text=text,
            created_at=created_at,
            project_dir=project_dir,
        )
        self.todos[todo.todo_id] = todo
        self.save()
        return todo

    def delete_todo(self, todo_id: str) -> None:
        if self.todos.pop(todo_id, None) is not None:
            self.save()

    def set_todo_done(self, todo_id: str, done_at: str) -> None:
        """done_at empty string reopens the todo."""
        todo = self.todos.get(todo_id)
        if todo is not None:
            todo.done_at = done_at
            self.save()

    def link_todo_session(self, todo_id: str, session_id: str) -> None:
        todo = self.todos.get(todo_id)
        if todo is not None and session_id not in todo.session_ids:
            todo.session_ids.append(session_id)
            self.save()

    def set_todo_worktree(self, todo_id: str, worktree: str) -> None:
        todo = self.todos.get(todo_id)
        if todo is not None:
            todo.worktree = worktree
            self.save()
