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

from .agent_status import fetch_agent_states
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
    pane_shell_count,
    pane_shows_prompt,
    pane_shows_working,
)


class SessionState(Enum):
    WORKING = "working"
    NEEDS_INPUT = "needs input"  # blocked on a human: permission / question
    EXTERNAL_UPDATE = "external update"  # was waiting; the PR changed (comment, review, or any other update)
    NEEDS_REVIEW = "needs review"  # Claude finished; no human has looked yet
    SHELL_RUNNING = "shell running"  # idle, but a shell it started is still live
    MONITORING = "monitoring"  # idle, but Claude's own Monitor is watching
    BACKGROUND = "background"  # idle, but a backgrounded command/agent runs
    SNOOZED = "snoozed"  # explicitly deferred by a human (s) until a set time
    WAITING_EXTERNAL = "waiting"  # done here; parked on a PR (w)
    DONE = "done"  # a human explicitly accepted the result
    STOPPED = "stopped"  # ended without completing normally


# Lower number = needs the human sooner. Used by the queue view and for
# sorting within groups. Monitoring/background/waiting are all "Claude (or
# the world) is on it" — lower priority than a genuine needs-review.
# external_update outranks a plain needs-review: something specific
# happened on the PR (a comment, a review, a push), not just "Claude
# happened to finish quietly".
# shell_running outranks monitoring: a shell Claude left running is more
# directly "your problem" than an automated Monitor watch.
# snoozed sits below working (an explicit, timed "leave me alone" always
# loses to something actually happening right now) but above waiting (a
# deliberate defer is still more "yours" than a PR parked on someone else).
ATTENTION_ORDER = {
    SessionState.NEEDS_INPUT: 0,
    SessionState.EXTERNAL_UPDATE: 1,
    SessionState.NEEDS_REVIEW: 2,
    SessionState.SHELL_RUNNING: 3,
    SessionState.MONITORING: 4,
    SessionState.BACKGROUND: 5,
    SessionState.WORKING: 6,
    SessionState.SNOOZED: 7,
    SessionState.WAITING_EXTERNAL: 8,
    SessionState.STOPPED: 9,
    SessionState.DONE: 10,
}

_STATE_BY_VALUE = {state.value: state for state in SessionState}


def _migrate_near_complete_state_order(order_setting: list) -> list:
    """A persisted state_order missing only one or two states was, almost
    certainly, saved as a COMPLETE list before those states existed —
    confirmed live: SNOOZED sank to dead last, below DONE, for a store
    saved before it existed. Those get inserted at their canonical
    position (anchored to the nearest canonical neighbor the user's list
    already has) instead of appended after everything else.

    A genuinely small, deliberate custom order (a handful of states the
    user cares about, the rest left to default) is untouched — appending
    the remainder in canonical order there (attention_rank_map's own job)
    is exactly the documented Priority-tab behavior, and there'd be no
    reliable way to tell "new state" from "one the user just didn't list"
    once more than a couple are missing."""
    canonical = sorted(ATTENTION_ORDER, key=ATTENTION_ORDER.get)
    values = [str(v) for v in order_setting]
    known = {_STATE_BY_VALUE[v] for v in values if v in _STATE_BY_VALUE}
    missing = [state for state in canonical if state not in known]
    if not missing or len(missing) > 2:
        return list(order_setting)
    order = list(order_setting)
    for state in missing:
        index = canonical.index(state)
        anchor = next((s for s in reversed(canonical[:index]) if s in known), None)
        if anchor is not None:
            insert_at = values.index(anchor.value) + 1
        else:
            anchor = next((s for s in canonical[index + 1 :] if s in known), None)
            insert_at = values.index(anchor.value) if anchor is not None else len(order)
        order.insert(insert_at, state.value)
        values.insert(insert_at, state.value)
        known.add(state)
    return order


def attention_rank_map(order_setting) -> dict[SessionState, int]:
    """The effective priority of each state. A user-provided ordering (list
    of state value strings) wins; unknown names are ignored and missing
    states are appended in default order — so the setting can never brick
    the sort. See _migrate_near_complete_state_order for the one
    exception: a state new enough that an old, otherwise-complete saved
    order predates it."""
    if not isinstance(order_setting, list):
        return dict(ATTENTION_ORDER)
    order_setting = _migrate_near_complete_state_order(order_setting)
    rank: dict[SessionState, int] = {}
    for name in order_setting:
        state = _STATE_BY_VALUE.get(str(name))
        if state is not None and state not in rank:
            rank[state] = len(rank)
    for state in sorted(ATTENTION_ORDER, key=ATTENTION_ORDER.get):
        if state not in rank:
            rank[state] = len(rank)
    return rank


# If the transcript was written to this recently, Claude is mid-turn even if
# we can't see a live pane marker (writes happen every few seconds during a
# turn).
FRESH_WRITE_SECONDS = 20.0

# The "new conversation" terminal (`n`) tracks its session before any
# transcript exists — the user still has to actually type `claude` in the
# plain shell it opens. A tracked session with no transcript at all is
# otherwise "transcript missing"/STOPPED (something went wrong); within
# this grace window of being tracked, it's read as the ordinary, expected
# "hasn't started yet" instead.
NEW_TERMINAL_GRACE_SECONDS = 15 * 60.0


@dataclass
class SessionView:
    """Everything the UI needs to render one session anywhere."""

    session_id: str
    tracked: TrackedSession
    parsed: ParsedSession | None
    state: SessionState
    live: bool  # a tmux session is hosting this Claude session right now
    tmux_name: str = ""
    tmux_socket: str = ""
    attached: bool = False
    state_detail: str = ""  # e.g. the tool waiting for permission
    missing: bool = False  # transcript file disappeared
    # Only set when the session is waiting on a human (review / input):
    did_line: str = ""  # single line: the agent's most recent statement
    needs_line: str = ""  # single line: what it needs from you
    # Lineage, resolved against the snapshot (ids of *visible* sessions):
    child_ids: list[str] = field(default_factory=list)
    sibling_ids: list[str] = field(default_factory=list)
    # When this session last actually *changed* state (not last_activity,
    # which ticks forward on every token while genuinely WORKING) — sort
    # keys use this instead, so a session's position among same-rank peers
    # stays put while nothing meaningful has changed, only reshuffling
    # when a rank actually changes. See SessionRegistry._state_since.
    rank_stable_since: float = 0.0

    @property
    def parent_id(self) -> str:
        return self.tracked.parent_id

    @property
    def relation(self) -> str:
        return self.tracked.relation

    @property
    def jira_key(self) -> str:
        return self.tracked.jira_key

    @property
    def jira_status(self) -> str:
        return self.tracked.jira_status

    @property
    def jira_assignee(self) -> str:
        return self.tracked.jira_assignee

    @property
    def jira_url(self) -> str:
        if not self.tracked.jira_key:
            return ""
        from .jira import browse_url

        return browse_url(self.tracked.jira_key)

    @property
    def title(self) -> str:
        if self.tracked.label:
            return self.tracked.label
        if self.parsed and self.parsed.title:
            return self.parsed.title
        return self.session_id[:8]

    @property
    def project_dir(self) -> str:
        """Where the session started — stable, used for grouping."""
        if self.parsed and self.parsed.cwd:
            return self.parsed.cwd
        return self.tracked.project_dir

    @property
    def work_dir(self) -> str:
        """Where the session is working NOW (follows Claude's own worktrees —
        EnterWorktree changes the cwd on subsequent records). This is what
        diffs, terminals, and the ctx keys should act on."""
        if self.parsed and self.parsed.last_cwd:
            return self.parsed.last_cwd
        return self.project_dir

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

    # Set by the registry from the (user-orderable) state priority.
    attention_rank: int = 99


EVENT_TOLERANCE = 2.0  # records may trail their hook event by a moment

# Claude Code's own discriminator on the Notification hook payload (docs:
# code.claude.com/docs/en/hooks#notification). It fires this ONE hook, with
# the same generic "Claude is waiting for your input" message text, both
# for a real blocking dialog and for a plain idle nudge once nothing has
# happened for ~60s — the message alone can't tell them apart (replicated
# live: an idle_prompt landing 60s after Stop with no pending tool call
# was misread as a fresh dialog before this field was checked). Only these
# types mean an actual human decision is pending.
BLOCKING_NOTIFICATION_TYPES = frozenset({
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
})
# idle_prompt, auth_success, elicitation_complete, elicitation_response,
# agent_completed, and any type this version of cagents doesn't yet know
# about, are all non-blocking — never a reason to show NEEDS_INPUT.


def derive_from_events(
    events: dict, parsed: ParsedSession, tracked: TrackedSession, now: float
) -> tuple[SessionState, str] | None:
    """Authoritative state from Claude Code's own hooks (sessions cagents
    spawned carry Notification/Stop/UserPromptSubmit hooks that stamp an
    events file). Returns None when the events are silent or superseded by
    newer transcript activity — the heuristics take over then."""
    conversation_ts = parsed.last_timestamp.timestamp() if parsed.last_timestamp else 0.0

    def valid(kind: str) -> float:
        ts = events.get(kind)
        if not isinstance(ts, (int, float)) or ts <= 0:
            return 0.0
        # A hook event is consumed once the conversation moves past it
        # (e.g. the permission it announced was granted).
        if conversation_ts > ts + EVENT_TOLERANCE:
            return 0.0
        return float(ts)

    t_notif = valid("Notification")
    t_stop = valid("Stop")
    t_submit = valid("UserPromptSubmit")
    latest = max(t_notif, t_stop, t_submit)
    if latest <= 0:
        return None
    if t_notif == latest:
        notification_type = events.get("notification_type")
        if isinstance(notification_type, str) and notification_type:
            # Authoritative: cagents-ctx stamped exactly what Claude Code
            # told it this notification was.
            blocking = notification_type in BLOCKING_NOTIFICATION_TYPES
        else:
            # Older Claude Code build, or a payload that didn't carry the
            # field — fall back to the best available signal: a real
            # dialog always has an unresolved tool call behind it.
            blocking = parsed.pending_tool_use
        if blocking:
            message = str(events.get("message") or "waiting on you")
            return (SessionState.NEEDS_INPUT, message[:110])
        return _finished_state(parsed, tracked, now)
    if t_stop == latest:
        return _finished_state(parsed, tracked, now)
    return (SessionState.WORKING, _working_detail(parsed))


def derive_state(
    parsed: ParsedSession | None,
    tracked: TrackedSession,
    live: bool,
    pane_text: str = "",
    now: float | None = None,
    events: dict | None = None,
    agent_state: dict | None = None,
) -> tuple[SessionState, str]:
    """Derive the lifecycle state and a short human-readable detail.

    The rules, in the order they win:

    - no transcript at all, tracked within NEW_TERMINAL_GRACE_SECONDS
                                                       -> NEEDS_INPUT
      (the `n` "new conversation" terminal: tracked before `claude` was
      ever actually typed in it — read as "hasn't started yet", not
      "something went wrong")
    - `claude agents --json`'s own busy/waiting/idle for this session
      (see agent_status.py) — authoritative, independent of pane text or
      hooks; busy -> WORKING, waiting -> NEEDS_INPUT. idle alone isn't
      enough to pick a specific state (doesn't distinguish done /
      needs-review / monitoring / …), so it falls through to everything
      below exactly as if this signal weren't available.
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
        added = tracked.added_datetime()
        if added is not None and now - added.timestamp() < NEW_TERMINAL_GRACE_SECONDS:
            return (SessionState.NEEDS_INPUT, "waiting on you — run `claude` in its terminal")
        return (SessionState.STOPPED, "transcript missing")

    if live and agent_state:
        status = agent_state.get("status")
        if status == "busy" and not _lingering_background_activity(parsed, pane_text, now):
            return (SessionState.WORKING, _working_detail(parsed))
        if status == "waiting":
            return (SessionState.NEEDS_INPUT, str(agent_state.get("waitingFor") or "waiting on you"))
        # status == "idle", or "busy" but only because a shell/monitor from
        # an already-finished turn is still going (see
        # _lingering_background_activity): not specific enough on its own
        # — fall through to events/pane heuristics, which is what actually
        # surfaces SHELL_RUNNING/MONITORING instead of collapsing them
        # back into a generic WORKING.

    if live and events:
        from_events = derive_from_events(events, parsed, tracked, now)
        if from_events is not None:
            return from_events

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
            # An unanswered tool call with no visible dialog is a tool still
            # RUNNING (long quiet Bash, variable spinner text) — replicated
            # live: guessing "permission" here was the intermittent false
            # "needs you". Real dialogs are caught by the pane check above
            # (and by the Notification hook on sessions cagents spawns).
            return (SessionState.WORKING, _working_detail(parsed))
        if parsed.last_record_role == "user":
            # A user message with no reply and no writes: waiting to start,
            # or the human is mid-conversation at the prompt.
            return (SessionState.NEEDS_INPUT, "at the prompt")
        if parsed.last_record_role == "":
            # A live session with no conversation yet: it's waiting for you.
            return (SessionState.NEEDS_INPUT, "at the prompt")
        return _finished_state(parsed, tracked, now, pane_text)

    # Not live in any tmux we can see. If the transcript is being written
    # RIGHT NOW, some other host (e.g. cmux, a bare terminal) is running it:
    # that's working, not stopped — but cagents can't attach to it.
    conversation_ts = parsed.last_timestamp.timestamp() if parsed.last_timestamp else None
    if conversation_ts is not None and now - conversation_ts < FRESH_WRITE_SECONDS:
        return (SessionState.WORKING, "active outside cagents' tmux")
    if parsed.pending_tool_use or parsed.last_record_role == "user":
        return (SessionState.STOPPED, "ended mid-turn")
    if parsed.last_record_role == "":
        return (SessionState.STOPPED, "empty session")
    return _finished_state(parsed, tracked, now)


def _lingering_background_activity(
    parsed: ParsedSession, pane_text: str, now: float
) -> bool:
    """True when a shell, monitor, or backgrounded command started by an
    EARLIER, already-finished turn is still going. `claude agents
    --json`'s "busy" status doesn't distinguish this from an
    actively-generating turn — both keep the session's own agent process
    alive — so trusting "busy" at face value collapses the more specific
    SHELL_RUNNING/MONITORING/BACKGROUND states this project deliberately
    carries back into a generic WORKING (confirmed live: a session with a
    genuine background agent running showed WORKING because this check
    covered shells and monitors but not background_active). When this is
    true, derive_state falls through to the pane/transcript heuristics
    instead, which do make that distinction (see _finished_state)."""
    return (
        bool(pane_shell_count(pane_text))
        or parsed.monitor_running(now)
        or parsed.background_active
        or bool(parsed.pending_agents)
    )


def _finished_state(
    parsed: ParsedSession, tracked: TrackedSession, now: float, pane_text: str = ""
) -> tuple[SessionState, str]:
    reviewed = tracked.reviewed_datetime()
    last = parsed.last_timestamp
    if reviewed is not None and (last is None or reviewed >= last):
        detail = tracked.finished_reason or "done"
        return (SessionState.DONE, detail)
    snoozed_until = tracked.snoozed_datetime()
    if snoozed_until is not None and now < snoozed_until.timestamp():
        # Deliberately pure time-based, unlike waiting/external_update
        # below: snoozing means "leave me alone for exactly this long,"
        # not "until something happens" — new transcript activity while
        # snoozed does not wake it early. Only the deadline (or an
        # explicit un-snooze) does.
        return (SessionState.SNOOZED, f"snoozed until {snoozed_until.strftime('%H:%M')}")
    waiting = tracked.waiting_datetime()
    if waiting is not None and (last is None or waiting >= last):
        # Parked on the outside world; the PR poller resolves it. New local
        # activity makes the comparison fail -> back to needs-review.
        return (SessionState.WAITING_EXTERNAL, "waiting on PR")
    external_update = tracked.external_update_datetime()
    if external_update is not None and (last is None or external_update >= last):
        # Reopened by the PR poller: something happened on the PR — a
        # comment, a review, or any other change (commits, labels, edits)
        # — while this was parked waiting. Same self-expiring idiom as
        # waiting_since — the moment real new activity happens (you type
        # something, the session goes back to work), the comparison fails
        # on its own and this falls through to a plain needs-review/
        # working, no explicit clearing required.
        return (SessionState.EXTERNAL_UPDATE, tracked.finished_reason or "external update")
    # Long-lived side tasks make an idle prompt "not really needs you" —
    # and they survive new messages: only their own completion/timeout
    # (or, for monitors, the expiry deadline) ends them.
    shells = pane_shell_count(pane_text)
    if shells:
        return (SessionState.SHELL_RUNNING, f"{shells} shell{'s' if shells != 1 else ''} running")
    if parsed.monitor_running(now):
        return (SessionState.MONITORING, "Claude monitor active")
    if parsed.background_active:
        return (SessionState.BACKGROUND, "background task running")
    if parsed.pending_agents:
        # Claude's own sub-agent concurrency (pendingBackgroundAgentCount
        # on "system" records) — a background AGENT, not a `run_in_
        # background` bash command, but the same "idle here, something
        # else is still going" bucket (BACKGROUND covers both by design —
        # see the state's own docstring).
        n = parsed.pending_agents
        return (SessionState.BACKGROUND, f"{n} background agent{'s' if n != 1 else ''} running")
    detail = tracked.finished_reason or "finished, unreviewed"
    return (SessionState.NEEDS_REVIEW, detail)


# Transitions that should be structurally impossible without the
# transcript's own last-activity timestamp having actually advanced —
# i.e. without new conversation content genuinely having appeared. This is
# deliberately a short, high-confidence list, not a general "did every
# input change" check: plenty of transitions are legitimately time-elapsed
# with no new writes (WORKING -> NEEDS_REVIEW once the fresh-write window
# lapses, DONE -> WAITING_EXTERNAL as the PR poller reacts, ...). Each
# entry here is a case where skipping the check should never happen; a
# logged violation means a real bug in derive_state (or in what fed it),
# not a false alarm to be tuned away.
#
# Deliberately NOT watching anything -> WORKING here (confirmed live: a
# false positive fired the moment a user typed a message). derive_state's
# own priority order checks the LIVE PANE before ever touching the
# transcript's timestamps — hitting the "esc to interrupt" spinner alone
# is enough to justify WORKING, and Claude Code updates the pane before
# the transcript file is flushed to disk. So `parsed.last_timestamp`
# lagging behind is completely normal right after you type something; it
# is not evidence of anything wrong, and this checker has no access to
# pane text to tell the two cases apart. NEEDS_REVIEW has no such
# shortcut — it's only ever reached through _finished_state, purely from
# transcript/tracked-field comparisons — so DONE -> NEEDS_REVIEW stays a
# fully reliable, false-positive-free check on its own.
REQUIRES_NEW_ACTIVITY = {
    (SessionState.DONE, SessionState.NEEDS_REVIEW),
}


def check_state_invariant(
    previous: SessionState | None,
    previous_last_activity,
    view: "SessionView",
) -> str | None:
    """A human-readable violation description if `view`'s transition from
    `previous` looks impossible given what actually changed — e.g. a
    session read as reviewed/finished suddenly reads as WORKING again with
    no new transcript record to justify it. None when the transition is
    fine (including: not one of the watched pairs, or genuinely backed by
    new activity)."""
    if previous is None or previous == view.state:
        return None
    if (previous, view.state) not in REQUIRES_NEW_ACTIVITY:
        return None
    current_last_activity = view.parsed.last_timestamp if view.parsed else None
    advanced = current_last_activity is not None and (
        previous_last_activity is None or current_last_activity > previous_last_activity
    )
    if advanced:
        return None
    return (
        f"{previous.value} -> {view.state.value} with no new transcript activity "
        f"(last_activity still {current_last_activity})"
    )


def derive_did_needs(
    state: SessionState,
    detail: str,
    parsed: ParsedSession | None,
    pane_text: str = "",
) -> tuple[str, str]:
    """The two waiting-on-you lines. Deliberately empty while WORKING —
    a mid-turn 'did' would be stale the moment it rendered."""
    if state not in (SessionState.NEEDS_INPUT, SessionState.NEEDS_REVIEW, SessionState.EXTERNAL_UPDATE):
        return ("", "")
    did = parsed.last_assistant_text if parsed else ""
    if state == SessionState.NEEDS_INPUT:
        needs = extract_prompt_question(pane_text) or detail or "your input"
    elif state == SessionState.EXTERNAL_UPDATE:
        needs = f"{detail or 'external update'} — check github, then r to accept"
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


def _content_match(parsed: ParsedSession, pane_text: str) -> bool:
    """Does this pane actually display this conversation? Whitespace is
    stripped from both sides so terminal line-wrapping can't break the
    containment check."""
    if not pane_text:
        return False
    pane_norm = "".join(pane_text.split())
    for fragment in (parsed.last_assistant_text, parsed.title):
        frag_norm = "".join(fragment.split())[:60]
        if len(frag_norm) >= 12 and frag_norm in pane_norm:
            return True
    return False


def map_tmux_sessions(
    tracked_views: list[tuple[TrackedSession, ParsedSession | None]],
    tmux_sessions: list[TmuxSession],
    pane_text_fn=None,
) -> dict[str, TmuxSession]:
    """Map Claude session id -> hosting tmux session.

    Three tiers, strongest first:
    1. CAGENTS_SESSION_ID env var on the tmux session — exact.
    2. Pane cwd == session cwd; newest qualifying transcript claims it —
       *if* it's the only directory-matching candidate for that pane.
    3. Pane cwd is an *ancestor* of the session cwd — covers launching
       `claude` from e.g. $HOME for a project deeper in the tree.

    Regression (found live, reproduced): a shared, non-worktree checkout
    (several tracked sessions with the identical project_dir, several live
    tmux panes with the identical pane cwd — routine for a repo people
    don't always work in a dedicated worktree for) makes tier 2 exactly as
    ambiguous as tier 3's "parent directory shelters many unrelated
    sessions" case. So both tiers require the pane to actually display
    text from the transcript (content verification) *whenever more than
    one candidate matches* — newest-mtime-wins-by-default is a guess, and
    mapping the WRONG live session is far worse than showing a live one as
    dead. An unambiguous single-candidate match (the common case) still
    needs no verification.

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
            claimed.add(tmux.key)

    parsed_by_id = {t.session_id: p for t, p in tracked_views}

    def match_pass(dir_matches, verify_content: bool) -> None:
        for tmux in sorted(tmux_sessions, key=lambda t: t.created, reverse=True):
            if tmux.key in claimed:
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
            candidates.sort(reverse=True)
            # More than one directory-matching candidate for this pane is
            # exactly the "wrong live session" scenario tier 3 already
            # guards against — a shared (non-worktree) checkout used by
            # several tracked sessions hits this on tier 2 just as easily.
            # Verify regardless of the tier once there's real ambiguity;
            # newest-mtime-wins-by-default is a guess, not a match.
            must_verify = verify_content or len(candidates) > 1
            for _mtime, sid in candidates:
                if must_verify:
                    if pane_text_fn is None:
                        continue
                    if not _content_match(parsed_by_id[sid], pane_text_fn(tmux)):
                        continue
                result[sid] = tmux
                claimed.add(tmux.key)
                break

    match_pass(_same_dir, verify_content=False)
    match_pass(_is_ancestor_dir, verify_content=True)
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
        agents_runner=None,
    ):
        self.store = store
        self.tmux = tmux or TmuxClient()
        self.claude_dir = claude_dir or default_claude_dir()
        self.agents_runner = agents_runner  # injectable for `claude agents --json`
        # Debounce state: a WORKING session must look blocked on two
        # consecutive refreshes before we say "needs you" — a single frame
        # of prompt-looking pane content is usually transient.
        self._last_state: dict[str, SessionState] = {}
        self._input_streak: dict[str, int] = {}
        # When each session's currently-displayed state was last actually
        # entered — frozen while the state doesn't change, so list order
        # doesn't jitter from last_activity ticking forward every token.
        self._state_since: dict[str, float] = {}
        # Transcript locations resolved via the discover_sessions fallback.
        # Claude Code's EnterWorktree physically MOVES the transcript into
        # the worktree's encoded project dir (verified live), so sessions
        # that used it miss the canonical path forever — without a cache
        # that's a full ~/.claude/projects scan on every refresh.
        self._file_cache: dict[str, Path] = {}

    def refresh(self, now: float | None = None) -> Snapshot:
        import time

        now = time.time() if now is None else now
        tmux_sessions = self.tmux.list_sessions()
        agent_states = fetch_agent_states(runner=self.agents_runner)

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

        pane_cache: dict[str, str] = {}

        def pane_text_of(tmux: TmuxSession) -> str:
            if tmux.key not in pane_cache:
                pane_cache[tmux.key] = self.tmux.capture_pane(tmux.name, socket=tmux.socket)
            return pane_cache[tmux.key]

        mapping = map_tmux_sessions(pairs, tmux_sessions, pane_text_fn=pane_text_of)

        views: list[SessionView] = []
        for tracked, parsed in pairs:
            tmux = mapping.get(tracked.session_id)
            live = tmux is not None
            pane_text = pane_text_of(tmux) if tmux is not None else ""
            events = self._load_events(tracked.session_id)
            state, detail = derive_state(
                parsed, tracked, live, pane_text, now, events=events,
                agent_state=agent_states.get(tracked.session_id),
            )
            previous_state = self._last_state.get(tracked.session_id)
            state, detail = self._debounce(tracked.session_id, state, detail)
            if state != previous_state or tracked.session_id not in self._state_since:
                self._state_since[tracked.session_id] = now
            did_line, needs_line = derive_did_needs(state, detail, parsed, pane_text)
            views.append(
                SessionView(
                    session_id=tracked.session_id,
                    tracked=tracked,
                    parsed=parsed,
                    state=state,
                    live=live,
                    tmux_name=tmux.name if tmux else "",
                    tmux_socket=tmux.socket if tmux else "",
                    attached=tmux.attached if tmux else False,
                    state_detail=detail,
                    missing=parsed is None,
                    did_line=did_line,
                    needs_line=needs_line,
                    rank_stable_since=self._state_since[tracked.session_id],
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

        # Attention ranks: the user can reorder state priority in settings.
        # With time_ordered_queue on, every state ranks equal, so ordering
        # falls through to rank_stable_since: a session rises to the top
        # ONLY when its state actually changes (working -> needs review,
        # a new message arriving, ...). Active work stays near the top; a
        # backlog of long-unreviewed sessions sinks instead of pinning
        # itself above everything by state alone.
        if self.store.get_setting("time_ordered_queue"):
            for view in views:
                view.attention_rank = 0
        else:
            rank_map = attention_rank_map(self.store.get_setting("state_order"))
            for view in views:
                view.attention_rank = rank_map[view.state]

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

    def _load_events(self, session_id: str) -> dict | None:
        """Hook-stamped state events for sessions cagents spawned."""
        import json

        path = self.store.path.parent / "events" / f"{session_id}.json"
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _find_session_file(self, tracked: TrackedSession) -> Path | None:
        path = session_file_path(self.claude_dir, tracked.project_dir, tracked.session_id)
        if path.is_file():
            return path
        cached = self._file_cache.get(tracked.session_id)
        if cached is not None and cached.is_file():
            return cached
        # Fall back to a scan: the project dir encoding is lossy, the session
        # may have started in a subdirectory, and EnterWorktree moves the
        # transcript under the worktree's own encoded dir. Cache the hit —
        # rescanning every refresh is the expensive path (and the transcript
        # can move again on the next EnterWorktree, hence the is_file check).
        for found in discover_sessions(self.claude_dir, min_size=0):
            if found.session_id == tracked.session_id:
                self._file_cache[tracked.session_id] = found.path
                return found.path
        return None

    def discover_untracked(self) -> list[DiscoveredSession]:
        tracked_ids = set(self.store.sessions)
        return [s for s in discover_sessions(self.claude_dir) if s.session_id not in tracked_ids]
