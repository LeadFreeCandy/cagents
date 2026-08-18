"""The session model: merge Claude's own data, tmux liveness, and cagents'
review state into the lifecycle states of spec §6.

State is always derived fresh — never stored — so it can't drift out of
sync with what Claude actually did (spec §9).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .claude_data import (
    DiscoveredSession,
    ParsedSession,
    default_claude_dir,
    discover_sessions,
    parse_session_file,
    session_file_path,
)
from .store import Store, TrackedSession
from .tmuxctl import (
    TmuxClient,
    TmuxSession,
    extract_prompt_question,
    pane_shows_prompt,
    pane_shows_working,
)


class SessionState(Enum):
    WORKING = "working"
    NEEDS_INPUT = "needs input"  # blocked on a human: permission / question
    NEEDS_REVIEW = "needs review"  # Claude finished; no human has looked yet
    MONITORING = "monitoring"  # seen it, keeping an eye; new activity re-alerts
    DONE = "done"  # a human explicitly accepted the result
    STOPPED = "stopped"  # ended without completing normally


# Lower number = needs the human sooner. Used by the queue view and for
# sorting within groups.
ATTENTION_ORDER = {
    SessionState.NEEDS_INPUT: 0,
    SessionState.NEEDS_REVIEW: 1,
    SessionState.MONITORING: 2,
    SessionState.WORKING: 3,
    SessionState.STOPPED: 4,
    SessionState.DONE: 5,
}

# If the transcript was written to this recently, Claude is mid-turn even if
# we can't see a live pane marker (writes happen every few seconds during a
# turn).
FRESH_WRITE_SECONDS = 20.0


@dataclass
class SessionView:
    """Everything the UI needs to render one session anywhere."""

    session_id: str
    tracked: TrackedSession
    parsed: ParsedSession | None
    state: SessionState
    live: bool  # a tmux session is hosting this Claude session right now
    tmux_name: str = ""
    attached: bool = False
    state_detail: str = ""  # e.g. the tool waiting for permission
    missing: bool = False  # transcript file disappeared
    # Only set when the session is waiting on a human (review / input):
    did_line: str = ""  # single line: the agent's most recent statement
    needs_line: str = ""  # single line: what it needs from you
    # Lineage, resolved against the snapshot (ids of *visible* sessions):
    child_ids: list[str] = field(default_factory=list)
    sibling_ids: list[str] = field(default_factory=list)

    @property
    def parent_id(self) -> str:
        return self.tracked.parent_id

    @property
    def relation(self) -> str:
        return self.tracked.relation

    @property
    def title(self) -> str:
        if self.tracked.label:
            return self.tracked.label
        if self.parsed and self.parsed.title:
            return self.parsed.title
        return self.session_id[:8]

    @property
    def project_dir(self) -> str:
        if self.parsed and self.parsed.cwd:
            return self.parsed.cwd
        return self.tracked.project_dir

    @property
    def project_name(self) -> str:
        return Path(self.project_dir).name or self.project_dir

    @property
    def last_activity(self) -> datetime | None:
        if self.parsed is None:
            return None
        if self.parsed.last_timestamp is not None:
            return self.parsed.last_timestamp
        return datetime.fromtimestamp(self.parsed.mtime, tz=timezone.utc)

    @property
    def started(self) -> datetime | None:
        return self.parsed.first_timestamp if self.parsed else None

    @property
    def attention_rank(self) -> int:
        return ATTENTION_ORDER[self.state]


def derive_state(
    parsed: ParsedSession | None,
    tracked: TrackedSession,
    live: bool,
    pane_text: str = "",
    now: float | None = None,
) -> tuple[SessionState, str]:
    """Derive the lifecycle state and a short human-readable detail.

    The rules, in the order they win:

    - live pane showing a permission/question prompt  -> NEEDS_INPUT
    - live pane showing an in-progress turn           -> WORKING
    - a conversation record (user/assistant) written
      in the last few seconds, while live             -> WORKING
      (the *conversation clock*, never the file mtime: merely resuming or
      attaching a session touches the file without appending anything, and
      must not read as "working")
    - live with an unanswered tool call               -> NEEDS_INPUT
      (tool_use recorded, no result, no fresh writes: almost always a
      permission prompt; a genuinely long-running quiet tool shows its
      "esc to interrupt" marker in the pane and is caught above)
    - turn complete (assistant ended its turn):
        reviewed since the last activity              -> DONE
        otherwise                                     -> NEEDS_REVIEW
    - not live and mid-turn                           -> STOPPED
    """
    import time

    now = time.time() if now is None else now

    if parsed is None:
        return (SessionState.STOPPED, "transcript missing")

    if live:
        if pane_text:
            if pane_shows_prompt(pane_text):
                detail = "waiting on you"
                if parsed.pending_tool_use and parsed.pending_tool_name:
                    detail = f"permission: {parsed.pending_tool_name}"
                return (SessionState.NEEDS_INPUT, detail)
            if pane_shows_working(pane_text):
                return (SessionState.WORKING, _working_detail(parsed))
        conversation_ts = (
            parsed.last_timestamp.timestamp() if parsed.last_timestamp else None
        )
        if conversation_ts is not None and now - conversation_ts < FRESH_WRITE_SECONDS:
            return (SessionState.WORKING, _working_detail(parsed))
        if parsed.pending_tool_use:
            detail = "waiting on you"
            if parsed.pending_tool_name:
                detail = f"permission: {parsed.pending_tool_name}"
            return (SessionState.NEEDS_INPUT, detail)
        if parsed.last_record_role == "user":
            # A user message with no reply and no writes: waiting to start,
            # or the human is mid-conversation at the prompt.
            return (SessionState.NEEDS_INPUT, "at the prompt")
        if parsed.last_record_role == "":
            # A live session with no conversation yet: it's waiting for you.
            return (SessionState.NEEDS_INPUT, "at the prompt")
        return _finished_state(parsed, tracked)

    # Not live: the CLI process is gone.
    if parsed.pending_tool_use or parsed.last_record_role == "user":
        return (SessionState.STOPPED, "ended mid-turn")
    if parsed.last_record_role == "":
        return (SessionState.STOPPED, "empty session")
    return _finished_state(parsed, tracked)


def _finished_state(parsed: ParsedSession, tracked: TrackedSession) -> tuple[SessionState, str]:
    reviewed = tracked.reviewed_datetime()
    last = parsed.last_timestamp
    if reviewed is not None and (last is None or reviewed >= last):
        return (SessionState.DONE, "reviewed")
    monitoring = tracked.monitoring_datetime()
    if monitoring is not None and (last is None or monitoring >= last):
        # Watched, not resolved. Activity after the mark re-alerts (the
        # comparison above fails and it falls back to needs-review).
        return (SessionState.MONITORING, "watching")
    return (SessionState.NEEDS_REVIEW, "finished, unreviewed")


def derive_did_needs(
    state: SessionState,
    detail: str,
    parsed: ParsedSession | None,
    pane_text: str = "",
) -> tuple[str, str]:
    """The two waiting-on-you lines. Deliberately empty while WORKING —
    a mid-turn 'did' would be stale the moment it rendered."""
    if state not in (SessionState.NEEDS_INPUT, SessionState.NEEDS_REVIEW):
        return ("", "")
    did = parsed.last_assistant_text if parsed else ""
    if state == SessionState.NEEDS_INPUT:
        needs = extract_prompt_question(pane_text) or detail or "your input"
    else:
        needs = "your review — r to accept"
    return (did, needs)


def _working_detail(parsed: ParsedSession) -> str:
    if parsed.pending_tool_use and parsed.pending_tool_name:
        return f"running {parsed.pending_tool_name}"
    return "thinking"


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def _is_ancestor_dir(ancestor: str, descendant: str) -> bool:
    """True if `ancestor` is a strict parent directory of `descendant`."""
    if not ancestor or not descendant:
        return False
    try:
        a = os.path.realpath(ancestor)
        d = os.path.realpath(descendant)
    except OSError:
        a, d = ancestor, descendant
    return d.startswith(a.rstrip("/") + "/")


def map_tmux_sessions(
    tracked_views: list[tuple[TrackedSession, ParsedSession | None]],
    tmux_sessions: list[TmuxSession],
) -> dict[str, TmuxSession]:
    """Map Claude session id -> hosting tmux session.

    Three tiers, strongest first:
    1. CAGENTS_SESSION_ID env var on the tmux session — exact.
    2. Pane cwd == session cwd; newest qualifying transcript claims it.
    3. Pane cwd is an *ancestor* of the session cwd — covers launching
       `claude` from e.g. $HOME for a project deeper in the tree.

    In tiers 2–3 a transcript only qualifies if it was written after the
    tmux session was created (otherwise it's an older conversation that
    happens to share the directory).
    """
    result: dict[str, TmuxSession] = {}
    claimed: set[str] = set()

    by_id = {t.cagents_session_id: t for t in tmux_sessions if t.cagents_session_id}
    for tracked, _parsed in tracked_views:
        tmux = by_id.get(tracked.session_id)
        if tmux is not None:
            result[tracked.session_id] = tmux
            claimed.add(tmux.name)

    def match_pass(dir_matches) -> None:
        for tmux in sorted(tmux_sessions, key=lambda t: t.created, reverse=True):
            if tmux.name in claimed:
                continue
            candidates = []
            for tracked, parsed in tracked_views:
                if tracked.session_id in result or parsed is None:
                    continue
                cwd = parsed.cwd or tracked.project_dir
                if not dir_matches(tmux.pane_path, cwd):
                    continue
                if parsed.mtime < tmux.created - 5:
                    continue
                candidates.append((parsed.mtime, tracked.session_id))
            if candidates:
                candidates.sort(reverse=True)
                sid = candidates[0][1]
                result[sid] = tmux
                claimed.add(tmux.name)

    match_pass(_same_dir)
    match_pass(_is_ancestor_dir)
    return result


@dataclass
class Snapshot:
    """One consistent view of the world, built off the UI thread."""

    views: list[SessionView] = field(default_factory=list)
    generated_at: float = 0.0

    def by_id(self, session_id: str) -> SessionView | None:
        for view in self.views:
            if view.session_id == session_id:
                return view
        return None

    def counts(self) -> dict[SessionState, int]:
        counts: dict[SessionState, int] = {}
        for view in self.views:
            counts[view.state] = counts.get(view.state, 0) + 1
        return counts


class SessionRegistry:
    """Builds Snapshots. All I/O lives here so the app can run it in a
    worker thread; the UI only ever consumes immutable Snapshot objects."""

    def __init__(
        self,
        store: Store,
        tmux: TmuxClient | None = None,
        claude_dir: Path | None = None,
    ):
        self.store = store
        self.tmux = tmux or TmuxClient()
        self.claude_dir = claude_dir or default_claude_dir()
        # Debounce state: a WORKING session must look blocked on two
        # consecutive refreshes before we say "needs you" — a single frame
        # of prompt-looking pane content is usually transient.
        self._last_state: dict[str, SessionState] = {}
        self._input_streak: dict[str, int] = {}

    def refresh(self, now: float | None = None) -> Snapshot:
        import time

        now = time.time() if now is None else now
        tmux_sessions = self.tmux.list_sessions()

        pairs: list[tuple[TrackedSession, ParsedSession | None]] = []
        for tracked in list(self.store.sessions.values()):
            if tracked.archived:
                continue  # hidden from views; still in the store's history
            path = self._find_session_file(tracked)
            parsed: ParsedSession | None = None
            if path is not None:
                try:
                    parsed = parse_session_file(path)
                except OSError:
                    parsed = None
            pairs.append((tracked, parsed))

        mapping = map_tmux_sessions(pairs, tmux_sessions)

        views: list[SessionView] = []
        for tracked, parsed in pairs:
            tmux = mapping.get(tracked.session_id)
            live = tmux is not None
            pane_text = ""
            if tmux is not None:
                pane_text = self.tmux.capture_pane(tmux.name)
            state, detail = derive_state(parsed, tracked, live, pane_text, now)
            state, detail = self._debounce(tracked.session_id, state, detail)
            did_line, needs_line = derive_did_needs(state, detail, parsed, pane_text)
            views.append(
                SessionView(
                    session_id=tracked.session_id,
                    tracked=tracked,
                    parsed=parsed,
                    state=state,
                    live=live,
                    tmux_name=tmux.name if tmux else "",
                    attached=tmux.attached if tmux else False,
                    state_detail=detail,
                    missing=parsed is None,
                    did_line=did_line,
                    needs_line=needs_line,
                )
            )

        # Lineage: resolve forks/handoffs against what's visible.
        children: dict[str, list[str]] = {}
        for view in views:
            if view.tracked.parent_id:
                children.setdefault(view.tracked.parent_id, []).append(view.session_id)
        for view in views:
            view.child_ids = children.get(view.session_id, [])
            if view.tracked.parent_id:
                view.sibling_ids = [
                    sid for sid in children.get(view.tracked.parent_id, [])
                    if sid != view.session_id
                ]

        # Stable, human-friendly default order: project, then newest first.
        views.sort(
            key=lambda v: (
                v.project_dir,
                -(v.last_activity.timestamp() if v.last_activity else 0.0),
            )
        )
        return Snapshot(views=views, generated_at=now)

    def _debounce(
        self, session_id: str, state: SessionState, detail: str
    ) -> tuple[SessionState, str]:
        """Hold a WORKING -> NEEDS_INPUT flip for one extra refresh. First
        observations (startup) are trusted immediately."""
        previous = self._last_state.get(session_id)
        if (
            state == SessionState.NEEDS_INPUT
            and previous == SessionState.WORKING
            and self._input_streak.get(session_id, 0) < 1
        ):
            self._input_streak[session_id] = self._input_streak.get(session_id, 0) + 1
            return (SessionState.WORKING, detail)  # hold; confirm next pass
        self._input_streak.pop(session_id, None)
        self._last_state[session_id] = state
        return (state, detail)

    def _find_session_file(self, tracked: TrackedSession) -> Path | None:
        path = session_file_path(self.claude_dir, tracked.project_dir, tracked.session_id)
        if path.is_file():
            return path
        # Fall back to a scan: the project dir encoding is lossy, and the
        # session may have been started in a subdirectory.
        for found in discover_sessions(self.claude_dir, min_size=0):
            if found.session_id == tracked.session_id:
                return found.path
        return None

    def discover_untracked(self) -> list[DiscoveredSession]:
        tracked_ids = set(self.store.sessions)
        return [s for s in discover_sessions(self.claude_dir) if s.session_id not in tracked_ids]
