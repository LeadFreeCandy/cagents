"""The optional background states share the normal review lifecycle by default."""
from datetime import datetime, timezone

import pytest
from textual.widgets import OptionList

from cagents.app import CagentsApp
from cagents.sessions import ATTENTION_ORDER, SessionRegistry, SessionState
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from conftest import FakeTmux, SID1, TranscriptBuilder, ts_ago


@pytest.fixture
def activity_world(claude_dir, tmp_path, now):
    def make(kind):
        builder = TranscriptBuilder(SID1, "/proj/activity").user("Start the task")
        if kind in ("monitoring", "shell running"):
            builder.assistant_tool_use("tool1", "Monitor", {"description": "watch deploy"})
            builder.raw_tool_result("tool1", "Monitor started (task monitor1, timeout 600000ms).", ts=ts_ago(90))
        elif kind == "background command":
            builder.assistant_tool_use("tool1", "Bash", {"command": "build", "run_in_background": True})
            builder.raw_tool_result("tool1", "Command running in background with ID: build1.", ts=ts_ago(90))
        else:
            builder.raw({"type": "system", "sessionId": SID1, "pendingBackgroundAgentCount": 1})
        builder.assistant_text("Task started.", ts=ts_ago(60)).write(claude_dir, mtime=now - 60)
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/activity", ts_ago(120))
        tmux = FakeTmux()
        tmux.sessions.append(TmuxSession("activity", now - 180, now, False, 1, "/proj/activity", cagents_session_id=SID1))
        if kind == "shell running":
            tmux.panes["activity"] = "Sonnet 5 | ctx: 37%\n⏵⏵ auto mode on · 1 shell running · ← 1 agent"
        return store, tmux, SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    return make


@pytest.mark.parametrize("kind, separate_state", [
    ("monitoring", SessionState.MONITORING),
    ("background command", SessionState.BACKGROUND),
    ("background agent", SessionState.BACKGROUND),
    ("shell running", SessionState.SHELL_RUNNING),
])
def test_background_states_default_to_review_and_toggle_together(activity_world, now, kind, separate_state):
    store, tmux, registry = activity_world(kind)
    assert store.get_setting("background_activity_states") is False
    assert Store.load(store.path).get_setting("background_activity_states") is False
    view = registry.refresh(now=now).by_id(SID1)
    assert view.state == SessionState.NEEDS_REVIEW
    assert view.state_detail == "finished, unreviewed"
    assert "your review" in view.needs_line
    assert view.attention_rank == ATTENTION_ORDER[SessionState.NEEDS_REVIEW]

    store.set_setting("background_activity_states", True)
    assert Store.load(store.path).get_setting("background_activity_states") is True
    assert registry.refresh(now=now + 1).by_id(SID1).state == separate_state
    store.set_setting("background_activity_states", False)
    assert registry.refresh(now=now + 2).by_id(SID1).state == SessionState.NEEDS_REVIEW

    # Accepting the result still clears the review, despite the lingering task.
    store.mark_reviewed(SID1, datetime.fromtimestamp(now, timezone.utc).isoformat())
    assert registry.refresh(now=now + 3).by_id(SID1).state == SessionState.DONE


def test_background_changes_keep_review_transition_time_stable(activity_world, now):
    store, tmux, registry = activity_world("shell running")
    first = registry.refresh(now=now).by_id(SID1)
    # The shell finishes while the monitor remains: still the same review row.
    tmux.panes["activity"] = ""
    second = registry.refresh(now=now + 2).by_id(SID1)
    assert first.state == second.state == SessionState.NEEDS_REVIEW
    assert first.rank_stable_since == second.rank_stable_since == now
    assert registry._last_state[SID1] == SessionState.NEEDS_REVIEW


def test_foreground_work_and_input_are_not_collapsed(activity_world, now):
    store, tmux, registry = activity_world("monitoring")
    tmux.panes["activity"] = "✻ Running… (esc to interrupt)"
    assert registry.refresh(now=now).by_id(SID1).state == SessionState.WORKING
    tmux.panes["activity"] = "Do you want to proceed?\n❯ 1. Yes"
    registry.refresh(now=now + 2)  # input debounce
    assert registry.refresh(now=now + 4).by_id(SID1).state == SessionState.NEEDS_INPUT


@pytest.mark.asyncio
async def test_settings_toggle_recomputes_snapshot_and_persists(activity_world, claude_dir):
    store, tmux, registry = activity_world("monitoring")
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW
        await pilot.press("comma")
        await pilot.pause()
        settings = app.screen.query_one("#settings-list", OptionList)
        settings.highlighted = next(i for i in range(settings.option_count)
                                    if settings.get_option_at_index(i).id == "background_activity_states")
        await pilot.press("enter")
        await pilot.pause()
        assert Store.load(store.path).get_setting("background_activity_states") is True
        assert app.snapshot.by_id(SID1).state == SessionState.MONITORING
        await pilot.press("enter")
        await pilot.pause()
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW
        assert Store.load(store.path).get_setting("background_activity_states") is False
