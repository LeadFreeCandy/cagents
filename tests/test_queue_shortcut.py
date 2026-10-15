"""Ctrl+G reaches the queue from a native conversation without typing into it."""
import shlex
import sys
from datetime import datetime, timezone

import pytest

from cagents.app import CagentsApp
from cagents.claude_data import ParsedSession
from cagents.sessions import SessionState, SessionView, Snapshot, attention_rank_map
from cagents.sidecar import container_setup_commands
from cagents.store import Store
from cagents.views import QueueView, SessionList
from conftest import FakeTmux
from test_clipboard import terminal  # shared real three-layer tmux/PTY fixture
from test_review_fifo import _review_views


@pytest.mark.parametrize("start_view", ["queue", "grouped", "kanban"])
async def test_ctrl_g_selects_first_queue_row(tmp_path, monkeypatch, start_view):
    store = Store(tmp_path / "state.json")
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
    monkeypatch.setattr(app, "refresh_data", lambda: None)
    now = datetime.now(timezone.utc)
    views = []
    for i in range(20):
        t = store.track(f"session-{i}", "/proj", now.isoformat())
        p = ParsedSession(t.session_id, tmp_path / "unused", cwd="/proj", title=f"Task {i}", last_timestamp=now)
        views.append(SessionView(t.session_id, t, p, SessionState.NEEDS_REVIEW, live=False,
                                 rank_stable_since=float(i)))
    app.snapshot = Snapshot(views=views)
    async with app.run_test(size=(100, 15)) as pilot:
        app.query_one(QueueView).update_snapshot(app.snapshot)
        queue = app.query_one("#queue-list", SessionList)
        first = queue.get_option_at_index(0).id
        app.action_switch_view(start_view)
        await pilot.pause()
        queue.highlighted = queue.option_count - 1
        await pilot.pause()
        assert queue.highlighted_session_id != first
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.active_view_id == "queue"
        assert queue.highlighted_session_id == app.selected_session_id == first
        assert queue.has_focus and queue.scroll_y == 0


@pytest.mark.parametrize("key", [b"\x07", b"\x11"], ids=["ctrl+g", "ctrl+q"])
@pytest.mark.parametrize("zoomed", [False, True])
def test_ctrl_g_from_nested_conversation_reaches_rail_without_moving_focus(terminal, zoomed, key):
    """Ctrl+G (and Ctrl+Q, done and next) from inside a conversation reaches
    the rail app without the agent seeing it, and leaves the layout exactly
    as it was: the chat keeps focus and its width (or zoom) — the key switches
    the conversation shown, it does not switch you to the list."""
    t = terminal
    rail_input = t.directory / "rail-input"
    program = t.directory / "rail.py"
    program.write_text("import os,sys,tty\ntty.setraw(0)\nwith open(sys.argv[1], 'ab', buffering=0) as f:\n while True: f.write(os.read(0,4096))\n")
    viewer = t.tmux(t.outer, "display-message", "-p", "#{pane_id}").strip()
    t.tmux(t.outer, "split-window", "-h", "-b", "-l", "25",
           shlex.join([sys.executable, str(program), str(rail_input)]))
    for command in container_setup_commands():
        t.tmux(t.outer, *command)
    t.tmux(t.outer, "select-pane", "-t", viewer)
    if zoomed:
        t.tmux(t.outer, "resize-pane", "-Z", "-t", viewer)
    t.drain(.3)
    before = t.tmux(t.outer, "display-message", "-p", "#{pane_index}:#{window_zoomed_flag}:#{pane_width}").strip()
    assert before.startswith("1:1:" if zoomed else "1:0:")
    t.write(key)
    t.drain(.3)
    assert t.tmux(t.outer, "display-message", "-p", "#{pane_index}:#{window_zoomed_flag}:#{pane_width}").strip() == before
    assert rail_input.read_bytes() == key
    assert t.received.read_bytes() == b""


async def _press_ctrl_q(tmp_path, monkeypatch, state):
    store = Store(tmp_path / "state.json")
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
    monkeypatch.setattr(app, "refresh_data", lambda: None)
    views = _review_views(store, tmp_path, count=3)
    views[0].state = state
    ranks = attention_rank_map(store.get_setting("state_order"))
    for view in views:
        view.attention_rank = ranks[view.state]
    app.snapshot = Snapshot(views=views)
    async with app.run_test(size=(100, 15)) as pilot:
        app.query_one(QueueView).update_snapshot(app.snapshot)
        queue = app.query_one("#queue-list", SessionList)
        queue.highlighted = queue.get_option_index("session-0")
        await pilot.pause()
        assert app.selected_session_id == "session-0"
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running, "Ctrl+Q quit the app"
        return app, queue


async def test_ctrl_q_marks_the_conversation_done_and_goes_to_the_next(tmp_path, monkeypatch):
    app, queue = await _press_ctrl_q(tmp_path, monkeypatch, SessionState.NEEDS_REVIEW)
    assert Store.load(tmp_path / "state.json").sessions["session-0"].reviewed_at
    assert app.snapshot.by_id("session-0").state == SessionState.DONE
    assert queue.highlighted_session_id == app.selected_session_id == "session-1"


async def test_ctrl_q_leaves_a_working_conversation_where_it_is(tmp_path, monkeypatch):
    app, queue = await _press_ctrl_q(tmp_path, monkeypatch, SessionState.WORKING)
    assert not Store.load(tmp_path / "state.json").sessions["session-0"].reviewed_at
    assert queue.highlighted_session_id == app.selected_session_id == "session-0"
