"""agent_status.py: `claude agents --json`, Claude Code's own first-class
state API — and derive_state's use of it as the highest-priority signal,
ahead of hooks and pane text."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import SID1, SID2, TranscriptBuilder, ts_ago

from cagents.agent_status import fetch_agent_states
from cagents.claude_data import parse_session_file
from cagents.sessions import SessionRegistry, SessionState, derive_state
from cagents.store import Store, TrackedSession
from cagents.tmuxctl import TmuxSession


def _tracked(sid: str = SID1) -> TrackedSession:
    return TrackedSession(sid, "/proj/a", "2026-08-18T09:00:00+00:00")


# --------------------------------------------------------------- fetch ---


def test_fetch_agent_states_keys_by_session_id():
    payload = json.dumps([
        {"sessionId": SID1, "kind": "interactive", "status": "busy"},
        {"sessionId": SID2, "kind": "interactive", "status": "idle"},
        {"kind": "background", "state": "done"},  # no sessionId — dropped
    ])
    states = fetch_agent_states(runner=lambda args: payload)
    assert states[SID1]["status"] == "busy"
    assert states[SID2]["status"] == "idle"
    assert len(states) == 2


def test_fetch_agent_states_calls_the_documented_command():
    calls = []

    def runner(args):
        calls.append(args)
        return "[]"

    fetch_agent_states(runner=runner)
    assert calls[0] == ["claude", "agents", "--json", "--all"]


def test_fetch_agent_states_never_raises_on_failure():
    assert fetch_agent_states(runner=lambda args: (_ for _ in ()).throw(RuntimeError("boom"))) == {}
    assert fetch_agent_states(runner=lambda args: "not json") == {}
    assert fetch_agent_states(runner=lambda args: '{"not": "a list"}') == {}


# ---------------------------------------------------------- derive_state ---


class TestAgentStatePrecedence:
    def _parse(self, claude_dir, builder):
        import time

        return parse_session_file(builder.write(claude_dir, mtime=time.time() - 300))

    def test_busy_wins_even_over_a_stale_needs_input_notification(self, claude_dir, now):
        # The actual reported bug: a permission dialog fired earlier, got
        # approved by pressing a numbered choice (which never triggers a
        # fresh UserPromptSubmit hook — that only fires for typed
        # messages), so the stale Notification event is still technically
        # "latest" by hook-timestamp bookkeeping. `claude agents --json`
        # itself saying "busy" must override that and everything else.
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("go").assistant_text("working on it", ts=ts_ago(5))
        parsed = self._parse(claude_dir, b)
        events = {
            "UserPromptSubmit": now - 200, "Notification": now - 100,
            "notification_type": "permission_prompt", "message": "Claude needs your permission",
        }
        pane = "✻ Photosynthesizing… (1m 9s · ↓ 2.7k tokens)"
        state, _ = derive_state(
            parsed, _tracked(), live=True, pane_text=pane, now=now, events=events,
            agent_state={"status": "busy"},
        )
        assert state == SessionState.WORKING

    def test_waiting_status_wins_and_carries_waiting_for_as_detail(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("go").assistant_text("done", ts=ts_ago(300))
        parsed = self._parse(claude_dir, b)
        state, detail = derive_state(
            parsed, _tracked(), live=True, now=now,
            agent_state={"status": "waiting", "waitingFor": "permission prompt"},
        )
        assert state == SessionState.NEEDS_INPUT
        assert detail == "permission prompt"

    def test_idle_status_falls_through_to_existing_heuristics(self, claude_dir, now):
        # "idle" alone can't distinguish done/needs-review/monitoring/… —
        # it must behave exactly as if agent_state weren't passed at all.
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("go").assistant_text("done", ts=ts_ago(300))
        parsed = self._parse(claude_dir, b)
        with_idle, _ = derive_state(
            parsed, _tracked(), live=True, now=now, agent_state={"status": "idle"}
        )
        without, _ = derive_state(parsed, _tracked(), live=True, now=now, agent_state=None)
        assert with_idle == without == SessionState.NEEDS_REVIEW

    def test_missing_or_unmatched_agent_state_is_a_pure_noop(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("go").assistant_text("done", ts=ts_ago(300))
        parsed = self._parse(claude_dir, b)
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, agent_state={})
        assert state == SessionState.NEEDS_REVIEW

    def test_not_live_ignores_agent_state(self, claude_dir, now):
        # agent_state describes a real running process; a session cagents
        # can't currently attach to (no tmux pane) must not be forced into
        # WORKING just because some other stale entry happened to match.
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("go").assistant_text("done", ts=ts_ago(300))
        parsed = self._parse(claude_dir, b)
        state, _ = derive_state(
            parsed, _tracked(), live=False, now=now, agent_state={"status": "busy"}
        )
        assert state != SessionState.WORKING


# --------------------------------------------------------- registry wiring ---


def test_registry_wires_agent_state_into_derive_state(claude_dir: Path, tmp_path: Path, now: float):
    from conftest import FakeTmux

    sid = SID1
    # transcript file mtime must be >= the tmux session's created time
    # (map_tmux_sessions' liveness check) — the *message* timestamp
    # (ts_ago(300)) is what keeps this out of the fresh-write WORKING path
    TranscriptBuilder(sid, "/proj/a").user("go").assistant_text(
        "done", ts=ts_ago(300)
    ).write(claude_dir, mtime=now - 1)
    store = Store.load(tmp_path / "state.json")
    store.track(sid, "/proj/a", "2026-08-18T09:00:00+00:00")
    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(name="a", created=now - 60, activity=now, attached=False,
                    pane_pid=1, pane_path="/proj/a", socket="claude")
    )
    tmux.panes["a"] = ""

    payload = json.dumps([{"sessionId": sid, "kind": "interactive", "status": "busy"}])
    registry = SessionRegistry(
        store, tmux=tmux, claude_dir=claude_dir, agents_runner=lambda args: payload
    )
    snap = registry.refresh(now=now)
    assert snap.by_id(sid).state == SessionState.WORKING
