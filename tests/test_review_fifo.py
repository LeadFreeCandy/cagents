"""Needs-review as a FIFO queue (review_oldest_first, default on): the
longest-waiting conversation sits on top, and Ctrl+G on a needs-review
conversation counts it as freshly updated so it moves to the back of the line."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from cagents.app import CagentsApp
from cagents.claude_data import ParsedSession
from cagents.sessions import SessionState, SessionView, Snapshot
from cagents.store import Store, TrackedSession
from cagents.views import QueueView, SessionList, attention_sort_key
from conftest import FakeTmux

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _review_views(store, tmp_path, count=5, fifo=True):
    """count needs-review sessions; session-i finished i minutes after session-0."""
    views = []
    for i in range(count):
        t = store.track(f"session-{i}", "/proj", NOW.isoformat())
        p = ParsedSession(t.session_id, tmp_path / "unused", cwd="/proj", title=f"Task {i}",
                          last_timestamp=NOW + timedelta(minutes=i))
        views.append(SessionView(t.session_id, t, p, SessionState.NEEDS_REVIEW, live=False,
                                 rank_stable_since=1000.0, review_fifo=fifo))
    return views


class TestStore:
    def test_review_bumped_at_round_trips(self):
        tracked = TrackedSession("sid", "/proj", NOW.isoformat(), review_bumped_at="2026-09-12T12:05:00+00:00")
        again = TrackedSession.from_dict("sid", json.loads(json.dumps(tracked.to_dict())))
        assert again.review_bumped_at == "2026-09-12T12:05:00+00:00"
        assert TrackedSession.from_dict("sid", {"project_dir": "/p", "added_at": ""}).review_bumped_at == ""

    def test_bump_review_persists(self, tmp_path):
        store = Store(tmp_path / "state.json")
        store.track("sid", "/proj", NOW.isoformat())
        store.bump_review("sid", "2026-09-12T12:05:00+00:00")
        assert Store.load(tmp_path / "state.json").sessions["sid"].review_bumped_at == "2026-09-12T12:05:00+00:00"
        store.bump_review("missing", "2026-09-12T12:05:00+00:00")  # no-op, no crash

    def test_setting_defaults_on_and_is_listed(self, tmp_path):
        from cagents.modals import SETTINGS_META

        store = Store(tmp_path / "state.json")
        assert store.get_setting("review_oldest_first") is True
        assert "review_oldest_first" in {key for key, _label, _help in SETTINGS_META}


class TestSortKey:
    def test_fifo_orders_oldest_response_first(self, tmp_path):
        views = _review_views(Store(tmp_path / "state.json"), tmp_path)
        views.reverse()  # input order must not matter
        ordered = sorted(views, key=attention_sort_key)
        assert [v.session_id for v in ordered] == [f"session-{i}" for i in range(5)]

    def test_bumped_session_moves_to_the_back(self, tmp_path):
        views = _review_views(Store(tmp_path / "state.json"), tmp_path)
        views[0].review_bumped_at = (NOW + timedelta(minutes=30)).timestamp()
        ordered = sorted(views, key=attention_sort_key)
        assert [v.session_id for v in ordered] == ["session-1", "session-2", "session-3", "session-4", "session-0"]

    def test_new_response_after_bump_still_counts(self, tmp_path):
        """A conversation that answers again after being bumped lines up by
        whichever is later — bump or response — never earlier than either."""
        views = _review_views(Store(tmp_path / "state.json"), tmp_path, count=3)
        views[0].review_bumped_at = (NOW + timedelta(minutes=1, seconds=30)).timestamp()
        views[0].parsed.last_timestamp = NOW + timedelta(minutes=10)
        ordered = sorted(views, key=attention_sort_key)
        assert [v.session_id for v in ordered] == ["session-1", "session-2", "session-0"]

    def test_setting_off_keeps_newest_first(self, tmp_path):
        views = _review_views(Store(tmp_path / "state.json"), tmp_path, fifo=False)
        views[0].review_bumped_at = (NOW + timedelta(minutes=30)).timestamp()  # ignored when off
        ordered = sorted(views, key=attention_sort_key)
        assert [v.session_id for v in ordered] == [f"session-{i}" for i in (4, 3, 2, 1, 0)]

    def test_other_states_keep_newest_first(self, tmp_path):
        views = _review_views(Store(tmp_path / "state.json"), tmp_path, count=3)
        for v in views:
            v.state = SessionState.NEEDS_INPUT
        ordered = sorted(views, key=attention_sort_key)
        assert [v.session_id for v in ordered] == ["session-2", "session-1", "session-0"]


class TestRegistryFlag:
    def _views(self, tmp_path, claude_dir, store):
        from cagents.sessions import SessionRegistry
        from conftest import TranscriptBuilder

        TranscriptBuilder("s1", "/proj").user("go").assistant_text("done").write(claude_dir)
        store.track("s1", "/proj", NOW.isoformat())
        store.sessions["s1"].review_bumped_at = "2026-09-12T12:05:00+00:00"
        return SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir).refresh().views

    def test_registry_sets_flag_and_bump_from_store(self, tmp_path, claude_dir):
        store = Store(tmp_path / "state.json")
        (view,) = self._views(tmp_path, claude_dir, store)
        assert view.review_fifo is True
        assert view.review_bumped_at == datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc).timestamp()

    @pytest.mark.parametrize("setting", ["review_oldest_first_off", "time_ordered_queue"])
    def test_registry_clears_flag(self, tmp_path, claude_dir, setting):
        store = Store(tmp_path / "state.json")
        if setting == "time_ordered_queue":
            store.set_setting("time_ordered_queue", True)
        else:
            store.set_setting("review_oldest_first", False)
        (view,) = self._views(tmp_path, claude_dir, store)
        assert view.review_fifo is False


class TestCtrlG:
    async def _run(self, tmp_path, monkeypatch, fifo):
        store = Store(tmp_path / "state.json")
        if not fifo:
            store.set_setting("review_oldest_first", False)
        app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
        monkeypatch.setattr(app, "refresh_data", lambda: None)
        app.snapshot = Snapshot(views=_review_views(store, tmp_path, fifo=fifo))
        async with app.run_test(size=(100, 15)) as pilot:
            app.query_one(QueueView).update_snapshot(app.snapshot)
            queue = app.query_one("#queue-list", SessionList)
            queue.highlighted = queue.get_option_index("session-2")
            await pilot.pause()
            assert app.selected_session_id == "session-2"
            await pilot.press("ctrl+g")
            await pilot.pause()
            return app, queue, [queue.get_option_at_index(i).id for i in range(queue.option_count)]

    async def test_ctrl_g_sends_current_review_to_the_back(self, tmp_path, monkeypatch):
        app, queue, order = await self._run(tmp_path, monkeypatch, fifo=True)
        assert order == ["session-0", "session-1", "session-3", "session-4", "session-2"]
        assert queue.highlighted_session_id == app.selected_session_id == "session-0"
        bumped = app.store.sessions["session-2"].review_bumped_at
        assert bumped and datetime.fromisoformat(bumped) > NOW + timedelta(minutes=4)
        assert Store.load(tmp_path / "state.json").sessions["session-2"].review_bumped_at == bumped
        assert not any(t.review_bumped_at for sid, t in app.store.sessions.items() if sid != "session-2")

    async def test_ctrl_g_repeats_walk_the_backlog(self, tmp_path, monkeypatch):
        """Pressing again from the row Ctrl+G landed on moves to the next-oldest."""
        store = Store(tmp_path / "state.json")
        app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
        monkeypatch.setattr(app, "refresh_data", lambda: None)
        bells = []
        monkeypatch.setattr(app, "bell", lambda: bells.append(1))
        app.snapshot = Snapshot(views=_review_views(store, tmp_path, count=3))
        async with app.run_test(size=(100, 15)) as pilot:
            app.query_one(QueueView).update_snapshot(app.snapshot)
            queue = app.query_one("#queue-list", SessionList)
            queue.highlighted = queue.get_option_index("session-2")
            await pilot.pause()
            landed = []
            for _ in range(4):
                await pilot.press("ctrl+g")
                await pilot.pause()
                landed.append(queue.highlighted_session_id)
            assert landed == ["session-0", "session-1", "session-2", "session-0"]
            assert not bells  # there was always somewhere else to go

    async def test_ctrl_g_does_not_bump_when_setting_off(self, tmp_path, monkeypatch):
        app, queue, order = await self._run(tmp_path, monkeypatch, fifo=False)
        assert order == [f"session-{i}" for i in (4, 3, 2, 1, 0)]
        assert queue.highlighted_session_id == "session-4"
        assert not any(t.review_bumped_at for t in app.store.sessions.values())

    async def test_ctrl_g_does_not_bump_non_review_sessions(self, tmp_path, monkeypatch):
        store = Store(tmp_path / "state.json")
        app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
        monkeypatch.setattr(app, "refresh_data", lambda: None)
        views = _review_views(store, tmp_path, count=3)
        views[2].state = SessionState.NEEDS_INPUT
        views[2].attention_rank = 0
        for v in views[:2]:
            v.attention_rank = 1
        app.snapshot = Snapshot(views=views)
        async with app.run_test(size=(100, 15)) as pilot:
            app.query_one(QueueView).update_snapshot(app.snapshot)
            queue = app.query_one("#queue-list", SessionList)
            queue.highlighted = queue.get_option_index("session-2")
            await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert queue.highlighted_session_id == "session-2"  # needs-input stays on top
            assert not any(t.review_bumped_at for t in store.sessions.values())


class TestCtrlGBell:
    """Ctrl+G rings the terminal bell when there is no other conversation in
    an alert state (needs input / needs review) to go to."""

    async def _press(self, tmp_path, monkeypatch, views, start):
        store = Store(tmp_path / "state.json")
        app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
        monkeypatch.setattr(app, "refresh_data", lambda: None)
        bells = []
        monkeypatch.setattr(app, "bell", lambda: bells.append(1))
        app.snapshot = Snapshot(views=views(store))
        async with app.run_test(size=(100, 15)) as pilot:
            app.query_one(QueueView).update_snapshot(app.snapshot)
            queue = app.query_one("#queue-list", SessionList)
            if start is not None:
                queue.highlighted = queue.get_option_index(start)
                await pilot.pause()
            await pilot.press("ctrl+g")
            await pilot.pause()
            return app, queue, bells

    async def test_bell_when_queue_has_no_alert_sessions(self, tmp_path, monkeypatch):
        def views(store):
            vs = _review_views(store, tmp_path, count=2)
            for v in vs:
                v.state = SessionState.DONE
            return vs
        app, queue, bells = await self._press(tmp_path, monkeypatch, views, None)
        assert bells == [1]
        assert app.active_view_id == "queue"  # still lands on the queue

    async def test_bell_when_empty(self, tmp_path, monkeypatch):
        _app, _queue, bells = await self._press(tmp_path, monkeypatch, lambda store: [], None)
        assert bells == [1]

    async def test_bell_when_only_alert_is_the_current_one(self, tmp_path, monkeypatch):
        app, queue, bells = await self._press(
            tmp_path, monkeypatch, lambda store: _review_views(store, tmp_path, count=1), "session-0")
        assert bells == [1]
        assert queue.highlighted_session_id == "session-0"

    async def test_no_bell_when_another_review_waits(self, tmp_path, monkeypatch):
        _app, queue, bells = await self._press(
            tmp_path, monkeypatch, lambda store: _review_views(store, tmp_path, count=2), "session-1")
        assert bells == []
        assert queue.highlighted_session_id == "session-0"

    async def test_no_bell_when_needs_input_waits(self, tmp_path, monkeypatch):
        def views(store):
            vs = _review_views(store, tmp_path, count=2)
            vs[0].state = SessionState.DONE
            vs[1].state = SessionState.NEEDS_INPUT
            return vs
        _app, queue, bells = await self._press(tmp_path, monkeypatch, views, "session-0")
        assert bells == []
        assert queue.highlighted_session_id == "session-1"


async def test_ctrl_g_while_pane_is_blurred_does_not_paint_focus(tmp_path, monkeypatch):
    """Ctrl+G arrives from the chat pane, so the rail app is blurred (tmux
    focus-events). It must move the highlight without focusing the list —
    focusing while blurred draws the focused border on an unfocused pane.
    Focus returns to the list once the pane is focused again."""
    store = Store(tmp_path / "state.json")
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
    monkeypatch.setattr(app, "refresh_data", lambda: None)
    app.snapshot = Snapshot(views=_review_views(store, tmp_path, count=3))
    async with app.run_test(size=(100, 15)) as pilot:
        app.query_one(QueueView).update_snapshot(app.snapshot)
        queue = app.query_one("#queue-list", SessionList)
        queue.highlighted = queue.get_option_index("session-2")
        await pilot.pause()
        app.app_focus = False  # terminal focus went to the conversation pane
        await pilot.pause()
        assert app.focused is None
        # Not pilot.press: the test pilot re-focuses the app on every key,
        # which a tmux send-keys from another pane never does.
        app.action_queue_top()
        await pilot.pause()
        assert app.active_view_id == "queue"
        assert queue.highlighted_session_id == "session-0"
        assert app.focused is None and not queue.has_focus
        app.app_focus = True  # focus comes back to the rail
        await pilot.pause()
        assert queue.has_focus
