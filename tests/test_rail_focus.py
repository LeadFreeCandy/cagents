"""The list's focused look must follow tmux's active pane, never Textual's
guess. Textual marks the app focused on ANY key while blurred — and a tmux
send-keys (Ctrl-G from the conversation pane) is exactly such a key."""
from datetime import datetime, timedelta, timezone

from textual import events

from cagents.app import CagentsApp
from cagents.claude_data import ParsedSession
from cagents.direct import DirectSidecar
from cagents.sessions import SessionState, SessionView, Snapshot
from cagents.sidecar import Sidecar
from cagents.store import Store
from cagents.views import QueueView, SessionList
from conftest import FakeTmux

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class FakeRail:
    """Only what the app asks a sidecar about terminal focus."""

    def __init__(self, focused):
        self.focused = focused
        self.queries = 0

    def rail_focused(self):
        self.queries += 1
        return self.focused


def _app(tmp_path, monkeypatch, rail):
    store = Store(tmp_path / "state.json")
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
    monkeypatch.setattr(app, "refresh_data", lambda: None)
    views = []
    for i in range(3):
        t = store.track(f"session-{i}", "/proj", NOW.isoformat())
        p = ParsedSession(t.session_id, tmp_path / "unused", cwd="/proj", title=f"Task {i}",
                          last_timestamp=NOW + timedelta(minutes=i))
        views.append(SessionView(t.session_id, t, p, SessionState.NEEDS_REVIEW, live=False,
                                 rank_stable_since=1000.0, review_fifo=True))
    app.snapshot = Snapshot(views=views)
    app.sidecar = rail
    return app


async def test_key_while_rail_pane_is_inactive_never_refocuses_the_list(tmp_path, monkeypatch):
    rail = FakeRail(focused=False)
    app = _app(tmp_path, monkeypatch, rail)
    async with app.run_test(size=(100, 15)) as pilot:
        app.query_one(QueueView).update_snapshot(app.snapshot)
        queue = app.query_one("#queue-list", SessionList)
        queue.highlighted = queue.get_option_index("session-2")
        await pilot.pause()
        app.app_focus = False  # tmux moved focus to the conversation pane
        await pilot.pause()
        assert app.focused is None
        await pilot.press("ctrl+g")  # delivered by tmux send-keys, pane still inactive
        await pilot.pause()
        assert queue.highlighted_session_id == "session-0"  # the key was handled
        assert app.app_focus is False
        assert app.focused is None and not queue.has_focus
        assert rail.queries >= 1
        # Real focus comes back (tmux made the rail active, then sent FocusIn).
        rail.focused = True
        app.post_message(events.AppFocus())
        await pilot.pause()
        assert app.app_focus is True and queue.has_focus


async def test_focus_in_while_rail_is_active_still_focuses(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, FakeRail(focused=True))
    async with app.run_test(size=(100, 15)) as pilot:
        await pilot.pause()
        app.app_focus = False
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.app_focus is True
        assert app.query_one("#queue-list", SessionList).has_focus


async def test_no_sidecar_keeps_textual_default(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, None)
    async with app.run_test(size=(100, 15)) as pilot:
        await pilot.pause()
        app.app_focus = False
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.app_focus is True


class TestSidecarQuery:
    def test_classic_asks_tmux_for_its_own_pane(self):
        calls = []
        def runner(args):
            calls.append(args)
            return "0\n"
        rail = Sidecar(runner=runner, own_pane="%7", work_runner=lambda a: "", session_runner=lambda a: "")
        assert rail.rail_focused() is False
        assert calls == [["display-message", "-p", "-t", "%7", "#{pane_active}"]]

    def test_direct_asks_its_server(self):
        calls = []
        def runner(args):
            calls.append(args)
            return "1"
        rail = DirectSidecar(runner=runner, own_pane="%3")
        assert rail.rail_focused() is True
        assert calls == [["display-message", "-p", "-t", "%3", "#{pane_active}"]]

    def test_failures_and_unknown_pane_fail_open(self):
        def boom(args):
            raise RuntimeError("no server")
        assert Sidecar(runner=boom, own_pane="%7", work_runner=lambda a: "", session_runner=lambda a: "").rail_focused() is True
        assert DirectSidecar(runner=lambda a: "", own_pane="").rail_focused() is True
