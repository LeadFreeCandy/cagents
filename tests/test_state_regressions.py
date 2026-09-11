"""State regressions from idle native sessions audited on 2026-09-10.

Fixtures preserve the record/UI shapes, without copying private conversations.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cagents.claude_data import ParsedSession, parse_session_file
from cagents.sessions import SessionRegistry, SessionState, derive_state
from cagents.store import Store, TrackedSession
from cagents.tmuxctl import TmuxSession
from conftest import FakeTmux, SID1, TranscriptBuilder

NOW = 1_789_094_400.0
IDLE_CLAUDE = "✻ Cooked for 0s · done Sunday\n────────\n❯ \n────────\n  Fable 5 │ ctx 71%\n  ⏵⏵ auto mode on"
IDLE_CODEX = "• Here and ready.\n\n› Ask Codex to do anything\n\n  gpt-6-astra xhigh · /proj"


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def tracked(provider="claude"):
    return TrackedSession(("codex:" if provider == "codex" else "") + SID1,
                          "/proj", iso(NOW - 3600))


def pending(provider="claude"):
    return ParsedSession(tracked(provider).session_id, Path("unused"),
                         last_timestamp=datetime.fromtimestamp(NOW - 300, timezone.utc),
                         last_record_role="assistant", pending_tool_use=True,
                         pending_tool_name="Bash", turn_state="running")


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("reviewed", [False, True])
def test_native_idle_overrides_old_running_records_and_hooks(provider, reviewed):
    t = tracked(provider)
    if reviewed:
        t.reviewed_at = iso(NOW - 100)
    state, _ = derive_state(
        pending(provider), t, True, IDLE_CODEX if provider == "codex" else IDLE_CLAUDE,
        now=NOW, events={"UserPromptSubmit": NOW - 300.1},
        agent_state={"type": "idle"} if provider == "codex" else {"status": "idle"})
    assert state == (SessionState.DONE if reviewed else SessionState.NEEDS_REVIEW)


def test_native_idle_does_not_show_twenty_seconds_of_work_after_a_reply():
    p = pending()
    p.last_timestamp = datetime.fromtimestamp(NOW - 1, timezone.utc)
    p.pending_tool_use = False
    p.last_stop_reason = "end_turn"
    assert derive_state(p, tracked(), True, IDLE_CLAUDE, now=NOW,
                        agent_state={"status": "idle"})[0] == SessionState.NEEDS_REVIEW


def ended_turn(claude_dir, *, truncated=False, age=300):
    b = TranscriptBuilder(SID1, "/proj")
    b.user("go", ts=iso(NOW - age - 10))
    b.assistant_tool_use("old-call", "Bash", {"command": "work"}, ts=iso(NOW - age - 9))
    if truncated:
        b.raw({"type": "padding", "data": "x" * 20000})
        b.tool_result("old-call", ts=iso(NOW - age - 8))
        b.raw({"type": "padding", "data": "x" * 20000})
    # The real failed turns ended with stop_sequence, then turn_duration.
    b.assistant_text("API Error: Unable to connect to API", ts=iso(NOW - age),
                     stop_reason="stop_sequence")
    b.raw({"type": "system", "subtype": "turn_duration", "durationMs": 202,
           "timestamp": iso(NOW - age + .01)})
    return b


@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("age", [1, 300])
def test_recorded_turn_end_clears_old_tools_and_submit_without_native_api(claude_dir, truncated, age):
    b = ended_turn(claude_dir, truncated=truncated, age=age)
    p = parse_session_file(b.write(claude_dir), head_bytes=2048, tail_bytes=2048)
    assert p.truncated == truncated
    assert not p.pending_tool_use
    # Completion bookkeeping must not manufacture new conversation activity.
    assert p.last_timestamp.timestamp() == NOW - age
    assert derive_state(p, tracked(), True, IDLE_CLAUDE, now=NOW,
                        events={"UserPromptSubmit": NOW - age - .2})[0] == SessionState.NEEDS_REVIEW


@pytest.mark.parametrize("signal", ["hook", "tool", "spinner"])
def test_new_work_after_recorded_completion_still_works(claude_dir, signal):
    b = ended_turn(claude_dir)
    if signal == "tool":
        b.user("another job", ts=iso(NOW - 180))
        b.assistant_tool_use("new-call", "Bash", {"command": "sleep 600"}, ts=iso(NOW - 179))
    p = parse_session_file(b.write(claude_dir))
    events = {"UserPromptSubmit": NOW - 1} if signal == "hook" else None
    pane = "✽ Baking… (45s · thinking some more)" if signal == "spinner" else ""
    assert derive_state(p, tracked(), True, pane, now=NOW, events=events)[0] == SessionState.WORKING


def test_background_agent_completion_does_not_finish_foreground_tool(claude_dir):
    b = TranscriptBuilder(SID1, "/proj").user("go", ts=iso(NOW - 300))
    b.assistant_tool_use("active", "Bash", {"command": "sleep 600"}, ts=iso(NOW - 299))
    b.raw({"type": "system", "subtype": "turn_duration", "durationMs": 100,
           "isSidechain": True, "timestamp": iso(NOW - 200)})
    p = parse_session_file(b.write(claude_dir))
    assert p.pending_tool_use
    assert derive_state(p, tracked(), True, now=NOW)[0] == SessionState.WORKING


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_quiet_work_and_real_approvals_are_preserved(provider):
    p, t = pending(provider), tracked(provider)
    busy = {"status": "busy"} if provider == "claude" else {"type": "active", "activeFlags": []}
    waiting = {"status": "waiting", "waitingFor": "permission prompt"} if provider == "claude" else {
        "type": "active", "activeFlags": ["waitingOnApproval"]}
    assert derive_state(p, t, True, now=NOW, agent_state=busy)[0] == SessionState.WORKING
    assert derive_state(p, t, True, now=NOW, agent_state=waiting)[0] == SessionState.NEEDS_INPUT
    assert derive_state(p, t, True, now=NOW)[0] == SessionState.WORKING


def test_codex_not_loaded_is_not_an_idle_signal():
    assert derive_state(pending("codex"), tracked("codex"), True, now=NOW,
                        agent_state={"type": "notLoaded"})[0] == SessionState.WORKING


def test_codex_idle_preserves_an_explicit_interruption():
    p = pending("codex")
    p.turn_state = "interrupted"
    assert derive_state(p, tracked("codex"), True, now=NOW,
                        agent_state={"type": "idle"})[0] == SessionState.STOPPED


def test_codex_scrollback_mention_of_interrupt_is_not_a_live_spinner():
    p = pending("codex")
    p.turn_state, p.pending_tool_use = "", False
    pane = "› What does esc to interrupt mean?\n" + IDLE_CODEX
    assert derive_state(p, tracked("codex"), True, pane, now=NOW)[0] == SessionState.NEEDS_REVIEW


def test_codex_current_spinner_still_works_without_runtime_api():
    p = pending("codex")
    p.turn_state, p.pending_tool_use = "", False
    pane = "• Working (3m 56s • esc to interrupt)\n\n› Ask Codex to do anything\n\n  gpt-6-astra xhigh · /proj"
    assert derive_state(p, tracked("codex"), True, pane, now=NOW)[0] == SessionState.WORKING


@pytest.mark.parametrize("policy,expected", [
    ("review", SessionState.DONE), ("snooze", SessionState.SNOOZED),
    ("waiting", SessionState.WAITING_EXTERNAL), ("monitor", SessionState.MONITORING),
    ("background", SessionState.BACKGROUND), ("shell", SessionState.SHELL_RUNNING),
])
def test_native_idle_preserves_finished_state_settings(policy, expected):
    p, t = pending(), tracked()
    pane = IDLE_CLAUDE
    if policy == "review":
        t.reviewed_at = iso(NOW - 1)
    elif policy == "snooze":
        t.snoozed_until = iso(NOW + 60)
    elif policy == "waiting":
        t.waiting_since = iso(NOW - 1)
    elif policy == "monitor":
        p.monitor_expiries = [NOW + 60]
    elif policy == "background":
        p.background_active = True
    else:
        pane += "\n  1 shell running"
    assert derive_state(p, t, True, pane, now=NOW, agent_state={"status": "idle"})[0] == expected


@pytest.mark.parametrize("before,after", [("busy", "completed"), ("idle", "running")])
def test_cached_runtime_state_cannot_override_new_transcript_activity(claude_dir, tmp_path, before, after):
    b = TranscriptBuilder(SID1, "/proj").user("go", ts=iso(NOW - 10))
    b.assistant_text("answer", ts=iso(NOW - 5))
    b.write(claude_dir, mtime=NOW - 1)
    store = Store(tmp_path / "state.json")
    store.sessions[SID1] = tracked()
    tmux = FakeTmux()
    tmux.sessions = [TmuxSession("agent", NOW - 60, NOW, False, 1, "/proj", cagents_session_id=SID1)]
    calls = []

    def runner(args):
        calls.append(args)
        return json.dumps([{"sessionId": SID1, "status": before}])

    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir, agents_runner=runner)
    registry.refresh(now=NOW)
    if after == "completed":
        # Only the duration record changed: the last assistant timestamp is
        # older than the cached busy sample, so that clock alone is insufficient.
        b.raw({"type": "system", "subtype": "turn_duration", "durationMs": 9000,
               "timestamp": iso(NOW + 1)})
    else:
        b.user("new work", ts=iso(NOW + 1))
    b.write(claude_dir, mtime=NOW + 1)
    state = registry.refresh(now=NOW + 2).by_id(SID1).state
    assert state == (SessionState.NEEDS_REVIEW if after == "completed" else SessionState.WORKING)
    assert len(calls) == 1  # no extra CLI subprocess per token


@pytest.mark.parametrize("signal", ["new_hook", "restarted_process"])
def test_cached_claude_status_cannot_override_new_hook_or_process(claude_dir, tmp_path, signal):
    ended_turn(claude_dir).write(claude_dir, mtime=NOW - 1)
    store = Store(tmp_path / "state.json")
    store.sessions[SID1] = tracked()
    tmux = FakeTmux()
    tmux.sessions = [TmuxSession("agent", NOW - 60, NOW, False, 1, "/proj", cagents_session_id=SID1)]
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir,
        agents_runner=lambda _: json.dumps([{"sessionId": SID1, "pid": 1,
                                             "status": "idle" if signal == "new_hook" else "busy"}]))
    registry.refresh(now=NOW)
    if signal == "new_hook":
        registry._load_events = lambda _: {"UserPromptSubmit": NOW + 1}
    else:
        tmux.sessions[0].pane_pid = 2
    assert registry.refresh(now=NOW + 2).by_id(SID1).state == (
        SessionState.WORKING if signal == "new_hook" else SessionState.NEEDS_REVIEW)


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_list_updates_working_idle_working_without_losing_selection(claude_dir, tmp_path, provider):
    from textual.app import App
    from cagents.views import QueueView, SessionList

    t = tracked(provider)
    store = Store(tmp_path / "state.json")
    store.sessions[t.session_id] = t
    runtime = {"busy": True}
    if provider == "claude":
        b = TranscriptBuilder(SID1, "/proj").user("go", ts=iso(NOW - 301))
        b.assistant_tool_use("quiet", "Bash", {"command": "sleep 600"}, ts=iso(NOW - 300))
        b.write(claude_dir, mtime=NOW - 1)
    else:
        path = tmp_path / "sessions" / f"rollout-test-{SID1}.jsonl"
        path.parent.mkdir()
        path.write_text("\n".join(json.dumps({"timestamp": iso(NOW - 300), "type": kind, "payload": p})
            for kind, p in [("session_meta", {"id": SID1, "cwd": "/proj"}),
                            ("event_msg", {"type": "user_message", "message": "go"}),
                            ("response_item", {"type": "function_call", "call_id": "quiet", "name": "exec"})]) + "\n")

    class NativeCodex:
        socket_path = tmp_path  # exists, without opening an actual socket

        def call(self, method, params):
            return {"thread": {"status": {"type": "active" if runtime["busy"] else "idle"}}}

    tmux = FakeTmux()
    tmux.sessions = [TmuxSession("agent", NOW - 60, NOW, False, 1, "/proj", cagents_session_id=t.session_id)]
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir, codex_dir=tmp_path,
        codex_client=NativeCodex(), agents_runner=lambda _: json.dumps([
            {"sessionId": SID1, "status": "busy" if runtime["busy"] else "idle"}]))

    class ListApp(App):
        compact = True

        def compose(self):
            yield QueueView()

    app = ListApp()
    app.store = store
    async with app.run_test(size=(90, 12)) as pilot:
        view = app.query_one(QueueView)
        listing = app.query_one(SessionList)
        for tick, busy in enumerate((True, False, True)):
            runtime["busy"] = busy
            snap = registry.refresh(now=NOW + tick * 10)
            assert snap.by_id(t.session_id).state == (SessionState.WORKING if busy else SessionState.NEEDS_REVIEW)
            view.update_snapshot(snap)
            await pilot.pause()
            assert listing.highlighted_session_id == t.session_id
            row = listing.get_option(t.session_id).prompt.plain
            assert ("●" in row) == busy
            assert ("✳" if provider == "claude" else "›") in row
