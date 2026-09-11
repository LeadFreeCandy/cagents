"""Idle policy, real UI interactions, durable completion order, and process targeting."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
import time

import pytest
from textual.widgets import OptionList

from cagents.app import CagentsApp
from cagents.claude_data import ParsedSession
from cagents.format import session_row
from cagents.lifecycle import apply_auto_done, duration_seconds, should_suspend
from cagents.modals import CommandModal
from cagents.sessions import SessionRegistry, SessionState, SessionView
from cagents.store import Store, TrackedSession
from cagents.tmuxctl import TmuxClient, TmuxSession
from cagents.views import SelectionChanged, attention_sort_key
from conftest import FakeTmux, SID1, SID2, TranscriptBuilder, ts_ago


def iso(when):
    return datetime.fromtimestamp(when, timezone.utc).isoformat()


def idle_view(now, days=8, state=SessionState.NEEDS_REVIEW):
    last = now - days * 86400
    tracked = TrackedSession(SID1, "/repo", iso(last - 100))
    parsed = ParsedSession(SID1, Path("/nonexistent/transcript"), mtime=last,
                           last_timestamp=datetime.fromtimestamp(last, timezone.utc))
    return SessionView(SID1, tracked, parsed, state, True, "agent", "private",
                       pane_id="%1", pane_pid=123)


def test_auto_done_default_retroactive_and_duration_boundary(tmp_path):
    now = int(time.time())
    assert Store.load(tmp_path / "old-state.json").get_setting("auto_done_duration") == "7d"
    view = idle_view(now, days=7)
    apply_auto_done(view, "7d", now - 0.001)
    assert not view.auto_done
    apply_auto_done(view, "7d", now)
    assert view.auto_done and view.state == SessionState.DONE
    assert view.done_at == now  # when placed in Done, not the old transcript date
    assert "Done (auto)" in session_row(view).plain
    assert "Done (auto)" in session_row(view, compact=True).plain
    assert should_suspend(view, now)  # already idle over an hour, including on upgrade


@pytest.mark.parametrize("state", [SessionState.WORKING, SessionState.SNOOZED])
def test_auto_done_preserves_active_work_and_snooze(state):
    view = idle_view(time.time(), state=state)
    apply_auto_done(view, "7d", time.time())
    assert view.state == state and not view.auto_done


def test_auto_done_off_recent_input_and_new_transcript_prevent_completion():
    now = time.time()
    view = idle_view(now)
    apply_auto_done(view, "off", now)
    assert not view.auto_done
    view.tracked.last_interacted_at = iso(now - 20)
    apply_auto_done(view, "7d", now)
    assert not view.auto_done
    view.tracked.last_interacted_at = ""
    view.parsed.last_timestamp = datetime.fromtimestamp(now - 30, timezone.utc)
    apply_auto_done(view, "7d", now)
    assert not view.auto_done
    assert duration_seconds("12h") == 43200
    assert duration_seconds("invalid") == 7 * 86400


def test_manual_and_auto_done_interleave_by_persisted_completion_time(tmp_path):
    now = time.time()
    auto = idle_view(now)
    auto.tracked.auto_done_at = iso(now - 100)
    auto.tracked = TrackedSession.from_dict(SID1, auto.tracked.to_dict())
    apply_auto_done(auto, "7d", now)
    manual = idle_view(now, days=50, state=SessionState.DONE)
    manual.tracked.reviewed_at = iso(now - 50)
    apply_auto_done(manual, "7d", now)
    older_manual = idle_view(now, days=1, state=SessionState.DONE)
    older_manual.tracked.reviewed_at = iso(now - 200)
    apply_auto_done(older_manual, "7d", now)
    for v in (auto, manual, older_manual):
        v.attention_rank = 10
        v.rank_stable_since = now  # dashboard reboot cannot reorder done
    assert sorted([auto, older_manual, manual], key=attention_sort_key) == [manual, auto, older_manual]


def test_suspend_requires_done_and_one_hour_without_real_activity():
    now = int(time.time())
    view = idle_view(now, days=1, state=SessionState.DONE)
    view.tracked.reviewed_at = iso(now - 3600)
    assert not should_suspend(view, now - 0.01)
    assert should_suspend(view, now)
    view.tracked.last_interacted_at = iso(now - 1)
    assert not should_suspend(view, now)
    view.tracked.last_interacted_at = ""
    for state in (SessionState.WORKING, SessionState.NEEDS_INPUT, SessionState.NEEDS_REVIEW):
        view.state = state
        assert not should_suspend(view, now)


class LifecycleTmux(FakeTmux):
    def __init__(self):
        super().__init__()
        self.replacements = []
        self.input_activity = {}

    def client_activity(self):
        return self.input_activity

    def replace_agent(self, name, sid, **kwargs):
        self.replacements.append((name, sid, kwargs))
        session = next(s for s in self.sessions if s.name == name)
        session.suspended = session.pane_dead = kwargs["command"] is None
        session.pane_pid += 1


@pytest.fixture
def idle_world(claude_dir, tmp_path, monkeypatch):
    now = time.time()
    root = tmp_path / "repo"
    root.mkdir()
    path = TranscriptBuilder(SID1, str(root)).user("old task", ts=ts_ago(9 * 86400)).assistant_text(
        "finished", ts=ts_ago(8 * 86400)).write(claude_dir, mtime=now - 8 * 86400)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, str(root), ts_ago(10 * 86400))
    tmux = LifecycleTmux()
    tmux.sessions.append(TmuxSession("agent", now - 10 * 86400, now, False, 123, str(root),
                                      cagents_session_id=SID1, pane_command="claude", pane_id="%1"))
    app = CagentsApp(store=store, tmux=tmux, claude_dir=claude_dir)
    monkeypatch.setattr(app, "_agent_bin", lambda p: f"/opt/bin/{p}")
    return app, store, tmux, path


async def test_first_launch_auto_done_suspends_and_hover_resumes_once(idle_world):
    app, store, tmux, path = idle_world
    before = path.read_bytes()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        view = app.snapshot.by_id(SID1)
        assert view.state == SessionState.DONE and view.auto_done and view.suspended
        saved = Store.load(store.path).sessions[SID1]
        assert saved.auto_done_at and saved.suspended_at
        assert len(tmux.replacements) == 1
        # Rebuilds and reasserted selections never reset the clock or wake it.
        for _ in range(3):
            app.on_selection_changed(SelectionChanged("queue", SID1))
            app.refresh_data()
            await pilot.pause()
        assert len(tmux.replacements) == 1
        assert not store.sessions[SID1].last_interacted_at
        await pilot.hover("#queue-list", offset=(8, 1))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(tmux.replacements) == 2
        assert "--resume" in tmux.replacements[-1][2]["command"]
        assert SID1 in tmux.replacements[-1][2]["command"]
        assert not app.snapshot.by_id(SID1).suspended
        assert not app.snapshot.by_id(SID1).auto_done
        assert store.sessions[SID1].last_interacted_at
        assert path.read_bytes() == before


async def test_real_client_input_prevents_suspension(idle_world):
    app, store, tmux, _ = idle_world
    tmux.input_activity[f"{tmux.create_socket}:agent"] = time.time()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert not tmux.replacements
        assert store.sessions[SID1].last_interacted_at


async def test_restart_key_preserves_conversation_and_uses_provider(idle_world):
    app, store, tmux, _ = idle_world
    store.set_setting("auto_done_duration", "off")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        assert len(tmux.replacements) == 1
        assert tmux.replacements[0][1] == SID1
        assert shlex.split(tmux.replacements[0][2]["command"])[1:4] == ["/opt/bin/claude", "--resume", SID1]
        view = replace(app.snapshot.by_id(SID1), session_id="codex:" + SID1,
                       tracked=TrackedSession("codex:" + SID1, str(Path.cwd()), iso(time.time())))
        command, _ = app._resume_command(view)
        assert shlex.split(command)[1:4] == ["env", f"CODEX_HOME={app.codex_dir}", "/opt/bin/codex"]
        assert ["resume", SID1] == shlex.split(command)[4:6]


async def test_settings_changes_duration_and_persists(idle_world):
    app, store, tmux, _ = idle_world
    store.set_setting("auto_done_duration", "off")
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        options = app.screen.query_one("#settings-list", OptionList)
        options.highlighted = next(i for i in range(options.option_count)
                                   if options.get_option_at_index(i).id == "auto_done_duration")
        await pilot.press("enter")
        await pilot.pause()
        assert Store.load(store.path).get_setting("auto_done_duration") == "1d"
        assert app.snapshot.by_id(SID1).auto_done


async def test_colon_restart_is_deterministic_and_skips_suspended(idle_world, monkeypatch):
    app, store, tmux, _ = idle_world
    store.set_setting("auto_done_duration", "off")
    finished = []
    monkeypatch.setattr(app, "_restart_all_finished", lambda errors: finished.append(errors))
    async with app.run_test() as pilot:
        await pilot.pause()
        store.track(SID2, str(Path.cwd()), ts_ago(8 * 86400))
        store.sessions[SID2].suspended_at = ts_ago(3600)
        tmux.sessions.append(replace(tmux.sessions[0], name="sleeper", cagents_session_id=SID2,
                                     suspended=True, pane_dead=True))
        await pilot.press("colon")
        assert isinstance(app.screen, CommandModal)
        await pilot.press(*"restart", "enter")
        await app.workers.wait_for_complete()
        assert finished == [[]]
        assert [row[0] for row in tmux.replacements] == ["agent"]
        assert not hasattr(app, "action_palette")


async def test_restart_failure_preserves_dashboard_and_reports_error(idle_world, monkeypatch):
    app, store, tmux, _ = idle_world
    store.set_setting("auto_done_duration", "off")
    notices = []
    monkeypatch.setattr(app, "notify", lambda message, **kw: notices.append(str(message)))
    def fail(*args, **kwargs):
        raise RuntimeError("agent changed")
    monkeypatch.setattr(tmux, "replace_agent", fail)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_restart_all()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.restart_requested
        assert any("Restart incomplete" in message and "agent changed" in message for message in notices)


async def test_keyboard_selection_wakes_and_startup_rebuild_does_not(idle_world):
    app, store, tmux, _ = idle_world
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert len(tmux.replacements) == 1
        await pilot.press("down")  # explicit keyboard visit, even on the only row
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert len(tmux.replacements) == 2


def test_suspended_codex_stays_done_without_polling_shared_server(tmp_path):
    from test_codex import rollout, message, rec, KEY, NOW, TS
    root = tmp_path / "codex"
    rollout(root, message("assistant", "Finished", phase="final_answer"),
            rec("event_msg", {"type": "task_complete"}))
    store = Store(tmp_path / "state.json")
    tracked = store.track(KEY, "/proj", TS)
    tracked.suspended_at = iso(NOW + 8 * 86400)
    tracked.auto_done_at = tracked.suspended_at
    tmux = LifecycleTmux()
    tmux.sessions.append(TmuxSession("codex", NOW, NOW, False, 123, "/proj",
                                    cagents_session_id=KEY, pane_id="%1", pane_dead=True, suspended=True))
    class UnusedServer:
        socket_path = tmp_path
        def call(self, *args):
            pytest.fail("Suspended conversations must not poll the shared app server")
    registry = SessionRegistry(store, tmux=tmux, codex_dir=root, codex_client=UnusedServer())
    view = registry.refresh(now=NOW + 9 * 86400).by_id(KEY)
    assert view.suspended and not view.live
    assert view.auto_done and view.state == SessionState.DONE
    assert view.tmux_name == "codex"


def test_codex_cleanup_event_does_not_reopen_done_or_change_its_position(tmp_path):
    from test_codex import rollout, message, rec, KEY, NOW, TS
    root = tmp_path / "codex"
    stopped = NOW + 8 * 86400
    path = rollout(root, message("assistant", "Finished", phase="final_answer"),
                   rec("event_msg", {"type": "turn_aborted"}, iso(stopped)))
    store = Store(tmp_path / "state.json")
    tracked = store.track(KEY, "/proj", TS)
    tracked.auto_done_at = iso(stopped - 100)
    tracked.suspended_at = tracked.idle_stopped_at = iso(stopped + 1)
    tracked.idle_activity_at = TS
    registry = SessionRegistry(store, tmux=LifecycleTmux(), codex_dir=root)
    view = registry.refresh(now=stopped + 2).by_id(KEY)
    assert view.auto_done and view.done_at == stopped - 100
    tracked.last_interacted_at = iso(stopped + 3)
    tracked.suspended_at = ""
    assert not registry.refresh(now=stopped + 4).by_id(KEY).auto_done
    # A real later abort remains a new event; only our shutdown is ignored.
    with path.open("a") as stream:
        import json
        stream.write(json.dumps(rec("event_msg", {"type": "turn_aborted"}, iso(stopped + 10))) + "\n")
    assert registry.refresh(now=stopped + 30).by_id(KEY).state == SessionState.STOPPED


def test_restart_remote_codex_stops_server_turn_before_replacing_tui(idle_world, monkeypatch):
    from cagents.codex_rpc import CodexClient
    app, store, tmux, path = idle_world
    view = idle_view(time.time())
    view.tracked = TrackedSession("codex:" + SID1, str(path.parent), ts_ago(60))
    view.session_id = view.tracked.session_id
    view.parsed.path = path
    sequence = []
    monkeypatch.setattr(tmux, "agent_start_command", lambda *a, **kw: "codex resume " + SID1 + " --remote unix:///tmp/test-rpc", raising=False)
    monkeypatch.setattr(CodexClient, "stop_thread_activity", lambda self, sid: sequence.append(("stop", sid, str(self.socket_path))))
    monkeypatch.setattr(tmux, "replace_agent", lambda *a, **kw: sequence.append(("replace", kw["command"])))
    app._replace_instance(view, "restart")
    assert sequence[0] == ("stop", SID1, "/tmp/test-rpc")
    assert sequence[1][0] == "replace" and "--remote" not in sequence[1][1]


def test_orphan_cleanup_never_signals_a_reused_pid(monkeypatch):
    from cagents.tmuxctl import _stop_orphaned_children
    monkeypatch.setattr("cagents.tmuxctl._process_table", lambda: {222: (1, "new birth"), 333: (1, "same birth")})
    monkeypatch.setattr("time.sleep", lambda _: None)
    signals = []
    monkeypatch.setattr("os.kill", lambda *a: signals.append(a))
    _stop_orphaned_children({222: "old birth", 333: "same birth"})
    assert signals and all(pid == 333 for pid, _ in signals)


def test_tmux_refuses_stale_or_shell_target_before_signalling(monkeypatch):
    client = TmuxClient(sockets=("test-only",))
    current = TmuxSession("agent", 1, 1, False, 222, "/repo", pane_id="%1", pane_command="claude")
    monkeypatch.setattr(client, "_list_on", lambda s: [current])
    monkeypatch.setattr(client, "get_session_env", lambda *a: SID1)
    signals = []
    monkeypatch.setattr("os.kill", lambda *a: signals.append(a))
    with pytest.raises(RuntimeError, match="changed"):
        client.replace_agent("agent", SID1, socket="test-only", pane_id="%1", pane_pid=111)
    current.pane_command = "zsh"
    with pytest.raises(RuntimeError, match="shell"):
        client.replace_agent("agent", SID1, socket="test-only", pane_id="%1", pane_pid=222)
    assert not signals


def test_tmux_suspend_targets_exact_pane_and_keeps_windows(monkeypatch):
    client = TmuxClient(sockets=("test-only",))
    current = TmuxSession("agent", 1, 1, False, 222, "/repo", pane_id="%9", pane_command="claude")
    monkeypatch.setattr(client, "_checked_agent_pane", lambda *a: current)
    calls = []
    monkeypatch.setattr(client, "_run", lambda socket, *args, **kw:
                        calls.append((socket, *args)) or subprocess.CompletedProcess(args, 0, "", ""))
    signals = []
    def signal(pid, sig):
        signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError
    monkeypatch.setattr("os.kill", signal)
    client.replace_agent("agent", SID1, socket="test-only", pane_id="%9", pane_pid=222)
    assert ("test-only", "set-option", "-p", "-t", "%9", "remain-on-exit", "on") in calls
    assert ("test-only", "respawn-pane", "-k", "-t", "%9", "/usr/bin/true") in calls
    assert calls[-1][-2:] == ("@cagents_suspended", "1")
    assert all(call[1] not in ("kill-session", "kill-server", "kill-window") for call in calls)


def test_dashboard_restart_reexecs_same_options(tmp_path, monkeypatch):
    from cagents.__main__ import main
    # main's setdefault values must not leak into later terminal-notifier tests.
    monkeypatch.setenv("CAGENTS_TERM_PROGRAM", "")
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", "")
    monkeypatch.setattr(CagentsApp, "run", lambda self: setattr(self, "restart_requested", True))
    calls = []
    monkeypatch.setattr("os.execv", lambda *a: calls.append(a))
    args = ["--fullscreen", "--store", str(tmp_path / "state.json"), "--codex-dir", str(tmp_path / "codex")]
    assert main(args) == 0
    assert calls[0][1][1:] == ["-m", "cagents", *args]
