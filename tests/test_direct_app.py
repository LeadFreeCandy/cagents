"""Application boundaries which change when agents share the UI's server."""
from types import SimpleNamespace
import os

from cagents.direct_app import DirectApp as CagentsApp
from cagents.store import Store
from test_direct_panes import direct


def test_quit_detaches_without_killing_agent_server(tmp_path, monkeypatch):
    calls = []
    sidecar = SimpleNamespace(detach=lambda: calls.append("detach"))
    app = CagentsApp(store=Store.load(tmp_path / "state.json"), sidecar=sidecar)
    monkeypatch.setattr("subprocess.run", lambda args, **kw: calls.append(args))
    app._teardown_container()
    assert calls == ["detach"], "quitting the rail must leave native agent processes running"


async def test_hover_leaves_native_pane_and_process_in_place(direct, tmp_path, monkeypatch):
    from cagents.app import VIEWER_COALESCE
    from cagents.views import SessionList
    from test_hover import hover_app, row_offset

    d = direct
    fixture, views = hover_app(tmp_path, monkeypatch)
    app = CagentsApp(store=fixture.store, tmux=d.tmux, sidecar=d.sidecar,
                    claude_dir=tmp_path / "claude")
    monkeypatch.setattr(app, "refresh_data", lambda: None)
    app.snapshot = fixture.snapshot
    panes = []
    for view in views:
        row = d.spawn(view.session_id)
        view.tmux_name, view.tmux_socket = row.name, d.socket
        view.pane_id, view.pane_pid = row.pane_id, row.pane_pid
        panes.append(row)

    async with app.run_test(size=(70, 35)) as pilot:
        app.action_switch_view("queue")
        await pilot.pause(VIEWER_COALESCE * 2)
        await app.workers.wait_for_complete()
        assert d.visible() == panes[0].pane_id
        listing = app.query_one("#queue-list", SessionList)
        offset = row_offset(listing, views[1].session_id)
        await pilot.hover(listing, offset=offset)
        await pilot.pause(VIEWER_COALESCE * 2)
        await app.workers.wait_for_complete()
        assert app.selected_session_id == views[0].session_id
        assert d.visible() == panes[0].pane_id
        await pilot.click(listing, offset=offset)
        await pilot.pause()
        assert d.visible() == panes[1].pane_id
        await pilot.press("up")
        await pilot.pause(VIEWER_COALESCE * 2)
        await app.workers.wait_for_complete()
        assert d.visible() == panes[0].pane_id
        for row in panes:
            assert d.run(["display-message", "-p", "-t", row.pane_id, "#{pane_pid}"]) == str(row.pane_pid)
            assert "UNSENT_DRAFT" in d.tmux.capture_pane(row.name)


def test_direct_entry_uses_separate_store_and_server(tmp_path, monkeypatch):
    from cagents.__main__ import main
    from cagents.app import CagentsApp as BaseApp
    observed = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # Register restoration even when the variables were initially absent.
    # delenv(..., raising=False) is a no-op then; main's setdefault would leak.
    monkeypatch.setenv("CAGENTS_TERM_PROGRAM", "")
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", "")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("CAGENTS_DIRECT", "1")
    monkeypatch.delenv("CAGENTS_SOCKET_SUFFIX", raising=False)
    monkeypatch.setattr(BaseApp, "run", lambda app: observed.append(app))
    assert main([]) == 0
    assert observed[0].store.path == tmp_path / ".local/share/cagents3/state.json"
    assert observed[0].tmux.create_socket == "cagents3"


def test_foreign_done_session_is_not_automatically_suspended(tmp_path, monkeypatch):
    from cagents.sessions import SessionState, SessionView, Snapshot
    from cagents.store import TrackedSession
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    tracked = TrackedSession("foreign", str(tmp_path), old, reviewed_at=old)
    view = SessionView("foreign", tracked, None, SessionState.DONE, True, "old-session", "cagents-sessions",
                       pane_id="%20", pane_pid=123)
    app = CagentsApp(store=Store.load(tmp_path / "state.json"))
    app.snapshot = SimpleNamespace(views=[view])
    stopped = []
    monkeypatch.setattr(app, "_begin_lifecycle", lambda *args: stopped.append(args))
    app._apply_idle_activity({})
    assert stopped == [], "the experimental app must not automatically stop the original app's agents"


async def test_textual_initializes_dashboard_exactly_once(tmp_path, monkeypatch):
    from cagents.app import CagentsApp as BaseApp
    calls = []
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(BaseApp, "on_mount", lambda self: calls.append(self))
    app = CagentsApp(store=Store.load(tmp_path / "state.json"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert calls == [app], "Textual dispatches inherited mount handlers automatically"


def test_launch_from_another_tmux_uses_its_own_server(tmp_path, monkeypatch):
    import pytest
    from cagents.__main__ import main
    from cagents.app import CagentsApp as BaseApp
    monkeypatch.setenv("CAGENTS_DIRECT", "1")
    monkeypatch.setenv("CAGENTS_SIDECAR", "1")
    monkeypatch.setenv("TMUX", "/tmp/original-tmux,123,0")
    monkeypatch.setenv("TMUX_PANE", "%5")
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(tmp_path))
    monkeypatch.setenv("CAGENTS_TERM_PROGRAM", "test")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(BaseApp, "run", lambda app: None)
    class Bootstrapped(Exception):
        pass
    def bootstrap(argv):
        raise Bootstrapped()
    monkeypatch.setattr("cagents.direct_cli.bootstrap", bootstrap)
    with pytest.raises(Bootstrapped):
        main(["--store", str(tmp_path / "state.json")])


def test_initial_state_copy_preserves_original_and_never_overwrites_new_state(tmp_path, monkeypatch):
    from cagents.direct_cli import seed_state
    original = tmp_path / "old/state.json"
    old = Store.load(original)
    old.track("existing", "/repo", "2026-09-01T00:00:00+00:00")
    before = original.read_bytes()
    monkeypatch.setattr("cagents.store.default_store_path", lambda: original)
    path = tmp_path / "new/state.json"
    seed_state(path)
    assert path.read_bytes() == before
    new = Store.load(path)
    new.sessions["existing"].label = "Changed only in cagents3"
    new.save()
    edited = path.read_bytes()
    seed_state(path)
    assert path.read_bytes() == edited
    assert original.read_bytes() == before
