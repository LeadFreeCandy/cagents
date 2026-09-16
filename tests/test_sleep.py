"""Done conversations wake only on Enter / → (browsing past them must not spin
up CLIs), and :sleep parks every idle conversation until it is visited."""
import shlex
import time
from pathlib import Path

import pytest

from cagents.app import CagentsApp
from cagents.modals import CommandModal
from cagents.sessions import SessionState
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from cagents.views import SelectionChanged
from conftest import SID1, SID2, SID3, TranscriptBuilder, ts_ago
from test_lifecycle import LifecycleTmux, idle_world  # noqa: F401 — fixture re-export


def resumes(tmux, sid):
    return [r for r in tmux.replacements if r[1] == sid and r[2]["command"] and "--resume" in r[2]["command"]]


def sleeps(tmux, sid):
    return [r for r in tmux.replacements if r[1] == sid and r[2]["command"] is None]


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


class TestDoneWakesOnlyExplicitly:
    async def test_hover_selection_and_keys_leave_done_asleep(self, idle_world):
        app, store, tmux, path = idle_world
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            view = app.snapshot.by_id(SID1)
            assert view.state == SessionState.DONE and view.suspended and len(tmux.replacements) == 1
            await pilot.hover("#queue-list", offset=(8, 1))
            await settle(app, pilot)
            await pilot.press("down", "j", "k")
            await settle(app, pilot)
            app.on_selection_changed(SelectionChanged("queue", SID1))
            app.refresh_data()
            await settle(app, pilot)
            assert len(tmux.replacements) == 1, "browsing must not wake a done conversation"
            assert not store.sessions[SID1].last_interacted_at
            assert app.snapshot.by_id(SID1).suspended and app.snapshot.by_id(SID1).auto_done

    async def test_enter_wakes_done_and_walks_in(self, idle_world):
        app, store, tmux, path = idle_world
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            assert len(tmux.replacements) == 1
            await pilot.press("enter")
            await settle(app, pilot)
            assert len(resumes(tmux, SID1)) == 1
            assert SID1 in tmux.replacements[-1][2]["command"]
            assert not app.snapshot.by_id(SID1).suspended
            assert not app.snapshot.by_id(SID1).auto_done  # explicit input reopens auto-done
            assert store.sessions[SID1].last_interacted_at
            assert tmux.attached_to  # Enter walks in after the resume

    async def test_right_arrow_wakes_done_and_walks_in(self, idle_world):
        app, store, tmux, path = idle_world
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            assert len(tmux.replacements) == 1
            await pilot.press("right")
            await settle(app, pilot)
            assert len(resumes(tmux, SID1)) == 1
            assert not app.snapshot.by_id(SID1).suspended
            assert tmux.attached_to

    async def test_asleep_placeholder_names_the_wake_keys(self, idle_world):
        app, store, tmux, path = idle_world
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            detail = app.snapshot.by_id(SID1).state_detail
            assert "hover" not in detail
            assert "Enter" in detail


@pytest.fixture
def busy_world(claude_dir, tmp_path, monkeypatch):
    """Three live conversations: needs review, working, and done (reviewed)."""
    now = time.time()
    root = tmp_path / "repo"
    root.mkdir()
    TranscriptBuilder(SID1, str(root)).user("go", ts=ts_ago(600)).assistant_text(
        "finished", ts=ts_ago(500)).write(claude_dir, mtime=now - 500)
    TranscriptBuilder(SID2, str(root)).user("go", ts=ts_ago(30)).assistant_tool_use(
        "t1", "Bash", {"command": "pytest"}, ts=ts_ago(2)).write(claude_dir, mtime=now - 2)
    TranscriptBuilder(SID3, str(root)).user("go", ts=ts_ago(7200)).assistant_text(
        "refactored", ts=ts_ago(7000)).write(claude_dir, mtime=now - 7000)
    store = Store.load(tmp_path / "state.json")
    for sid in (SID1, SID2, SID3):
        store.track(sid, str(root), ts_ago(86400))
    store.mark_reviewed(SID3, ts_ago(1200))
    tmux = LifecycleTmux()
    for i, sid in enumerate((SID1, SID2, SID3)):
        tmux.sessions.append(TmuxSession(f"agent{i}", now - 86400, now, False, 100 + i, str(root),
                                          cagents_session_id=sid, pane_command="claude", pane_id=f"%{i + 1}"))
    app = CagentsApp(store=store, tmux=tmux, claude_dir=claude_dir)
    monkeypatch.setattr(app, "_agent_bin", lambda p: f"/opt/bin/{p}")
    return app, store, tmux


async def type_command(app, pilot, text):
    await pilot.press("colon")
    assert isinstance(app.screen, CommandModal)
    await pilot.press(*text, "enter")
    await settle(app, pilot)


class TestSleepCommand:
    async def test_sleep_parks_idle_conversations_and_skips_working(self, busy_world):
        app, store, tmux = busy_world
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            states = {v.session_id: v.state for v in app.snapshot.views}
            assert states == {SID1: SessionState.NEEDS_REVIEW, SID2: SessionState.WORKING, SID3: SessionState.DONE}
            assert not tmux.replacements
            await type_command(app, pilot, "sleep")
            assert len(sleeps(tmux, SID1)) == 1 and len(sleeps(tmux, SID3)) == 1
            assert not [r for r in tmux.replacements if r[1] == SID2], "in-flight work is never killed"
            saved = Store.load(store.path).sessions
            assert saved[SID1].suspended_at and saved[SID3].suspended_at and not saved[SID2].suspended_at
            by_id = {v.session_id: v for v in app.snapshot.views}
            assert by_id[SID1].suspended and by_id[SID3].suspended and by_id[SID2].live

    async def test_visiting_a_slept_review_row_wakes_it_but_done_needs_enter(self, busy_world):
        app, store, tmux = busy_world
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await type_command(app, pilot, "sleep")
            assert len(tmux.replacements) == 2
            # Visit the slept needs-review row by keyboard: it wakes.
            queue = app.query_one("#queue-list")
            queue.highlighted = queue.get_option_index(SID3)  # park on done first
            await settle(app, pilot)
            assert not resumes(tmux, SID3), "landing on a slept done row does not wake it"
            while app.selected_session_id != SID1:
                await pilot.press("k")
                await pilot.pause()
            for _ in range(10):  # the visit is posted after the highlight settles
                await settle(app, pilot)
                if resumes(tmux, SID1):
                    break
            assert len(resumes(tmux, SID1)) == 1
            assert not app.snapshot.by_id(SID1).suspended
            # The done one still needs Enter.
            queue.highlighted = queue.get_option_index(SID3)
            await settle(app, pilot)
            assert not resumes(tmux, SID3)
            await pilot.press("enter")
            await settle(app, pilot)
            assert len(resumes(tmux, SID3)) == 1

    async def test_sleep_reports_when_nothing_is_awake(self, busy_world, monkeypatch):
        app, store, tmux = busy_world
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, **kw: notices.append(str(message)))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await type_command(app, pilot, "sleep")
            first = list(notices)
            assert any("2" in n and "sleep" in n.lower() for n in first), first
            assert any("1" in n and "working" in n.lower() for n in first), first
            notices.clear()
            await type_command(app, pilot, "sleep")
            assert any("nothing" in n.lower() or "0" in n for n in notices), notices

    async def test_unknown_command_lists_sleep(self, busy_world, monkeypatch):
        app, store, tmux = busy_world
        notices = []
        monkeypatch.setattr(app, "notify", lambda message, **kw: notices.append(str(message)))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await type_command(app, pilot, "bogus")
            assert any("sleep" in n and "restart" in n for n in notices), notices


def test_queue_top_binding_leaves_focus_and_zoom_alone():
    """Ctrl+G only delivers the key to the rail: it must not select the rail
    pane (the focus hook would re-size the split) nor un-zoom a full-width chat."""
    from cagents.sidecar import queue_top_binding

    script = " ".join(queue_top_binding())
    assert "send-keys -t :.0 C-g" in script
    assert "select-pane" not in script
    assert "resize-pane" not in script
    assert "zoomed" not in script


async def test_keyboard_visit_reports_the_row_landed_on(busy_world, monkeypatch):
    """j/k must count as a visit of the row you arrive on — not the one you
    left (which used to be woken instead, and the arrival only registered on
    the NEXT key)."""
    app, store, tmux = busy_world
    visits = []
    original = app._interact_session
    monkeypatch.setattr(app, "_interact_session", lambda sid: (visits.append(sid), original(sid)))
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        queue = app.query_one("#queue-list")
        queue.highlighted = queue.get_option_index(SID2)
        await settle(app, pilot)
        visits.clear()
        await pilot.press("k")  # SID2 (working) -> SID1 (needs review)
        await settle(app, pilot)
        assert app.selected_session_id == SID1
        assert visits == [SID1]
        await pilot.press("k")  # top row already: a re-visit of the same row
        await settle(app, pilot)
        assert visits == [SID1, SID1]
