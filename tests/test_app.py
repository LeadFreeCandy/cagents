"""End-to-end UI tests driven through Textual's pilot.

A fake tmux client and a temp Claude store give the app a fully
controlled world; no real tmux server or ~/.claude is touched. These run
with sidecar=None (fullscreen mode) unless a test injects a Sidecar —
sidecar-specific behavior lives in test_sidecar.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    SID1,
    SID2,
    SID3,
    FakeTmux,
    TranscriptBuilder,
    select_session,
    ts_ago,
    widget_text,
)

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry, SessionState
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from cagents.views import KanbanView, SessionList


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    """Three sessions in two projects, one live in tmux."""
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user("go").assistant_text(
        "done with auth"
    ).write(claude_dir, mtime=now - 900)
    TranscriptBuilder(SID2, "/proj/alpha").ai_title("Alpha: add tests").user("go").assistant_tool_use(
        "t1", "Bash", {"command": "pytest"}, ts=ts_ago(2)
    ).write(claude_dir, mtime=now - 2)
    TranscriptBuilder(SID3, "/proj/beta").ai_title("Beta: refactor").user("go").assistant_text(
        "refactored"
    ).write(claude_dir, mtime=now - 4000)

    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    store.track(SID2, "/proj/alpha", "2026-08-18T09:10:00+00:00")
    store.track(SID3, "/proj/beta", "2026-08-18T08:00:00+00:00")

    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(
            name="alpha", created=now - 300, activity=now, attached=False,
            pane_pid=42, pane_path="/proj/alpha", socket="claude",
        )
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux, claude_dir


async def test_startup_shows_queue_default(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # View 1 = queue is the default; no persisted view state exists.
        assert app.active_view_id == "queue"
        assert app.snapshot.views and len(app.snapshot.views) == 3
        by_id = {v.session_id: v for v in app.snapshot.views}
        assert by_id[SID2].state == SessionState.WORKING
        assert by_id[SID2].live is True
        assert by_id[SID2].tmux_socket == "claude"  # discovered on the wrapper socket
        assert by_id[SID1].state == SessionState.NEEDS_REVIEW
        assert by_id[SID3].state == SessionState.NEEDS_REVIEW

        grouped = app.query_one("#grouped-list", SessionList)
        assert grouped.option_count == 5  # 2 group headers + 3 rows
        summary = widget_text(app, "#summary")
        assert "3 sessions" in summary
        assert "1 working" in summary
        assert "2 review" in summary


async def test_navigation_updates_preview(world):
    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = app.selected_session_id
        await pilot.press("j")
        await pilot.pause()
        assert app.selected_session_id != first
        assert widget_text(app, "#preview-content")  # fullscreen mode renders internally


async def test_view_switching_cycles_three_views(world):
    app, *_ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.active_view_id == "queue"  # 1 = queue, the default
        await pilot.press("2")
        await pilot.pause()
        assert app.active_view_id == "grouped"
        await pilot.press("3")
        await pilot.pause()
        assert app.active_view_id == "kanban"
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_view_id == "queue"
        await pilot.press("1")
        await pilot.pause()
        assert app.active_view_id == "queue"


async def test_queue_orders_by_attention(world):
    app, store, tmux, claude_dir = world
    tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        queue = app.query_one("#queue-list", SessionList)
        assert queue.get_option_at_index(0).id == SID2  # needs input first
        assert app.snapshot.by_id(SID2).state == SessionState.NEEDS_INPUT


async def test_kanban_columns_and_arrow_navigation(world):
    app, *_ = world
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        working = app.query_one("#kb-working", SessionList)
        review = app.query_one("#kb-review", SessionList)
        assert working.option_count == 1
        assert review.option_count == 2
        kanban = app.query_one("#kanban", KanbanView)
        start = kanban.active_column
        await pilot.press("right")
        await pilot.pause()
        assert kanban.active_column != start  # arrows move between columns


async def test_attach_live_session_fullscreen(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)
        await pilot.pause()
        app.action_attach()
        await pilot.pause()
        # Attached on the session's own socket, statusline shown+restored.
        assert tmux.attached_to == [("alpha", "claude")]
        assert "status-on:alpha" in tmux.log and "status-off:alpha" in tmux.log


async def test_attach_dead_session_resumes_on_private_socket(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID3)
        await pilot.pause()
        # /proj/beta doesn't exist on disk -> loud failure, no attach
        app.action_attach()
        await pilot.pause()
        assert tmux.attached_to == []

        store.sessions[SID3].project_dir = "/tmp"
        view = app.snapshot.by_id(SID3)
        if view.parsed:
            view.parsed.cwd = "/tmp"
        app.action_attach()
        await pilot.pause()
        assert tmux.created and tmux.created[-1][1][:2] == ["--resume", SID3]
        # every spawn carries the state hooks (the anti-flap authority)
        assert "--settings" in tmux.created[-1][1]
        settings_json = tmux.created[-1][1][tmux.created[-1][1].index("--settings") + 1]
        assert "Notification" in settings_json and "Stop" in settings_json
        # Resume spawns on the PRIVATE socket (spawning next to a live
        # claude on a shared socket crashes it).
        assert tmux.attached_to[-1][1] == "cagents-sessions"


async def test_done_key_flow(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # needs review
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at != ""
        assert app.snapshot.by_id(SID1).state == SessionState.DONE
        await pilot.press("d")  # un-done
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at == ""
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW


async def test_done_refuses_while_working(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)  # working
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(0.1)
        assert store.sessions[SID2].reviewed_at == ""


async def test_rename(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press(*"auth work")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert store.sessions[SID1].label == "auth work"
        assert app.snapshot.by_id(SID1) is not None


async def test_untrack_with_confirm(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID3)
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("n")  # decline
        await pilot.pause(0.1)
        assert SID3 in store.sessions
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause(0.1)
        assert SID3 not in store.sessions


async def test_app_keys_do_not_fire_inside_modal(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("q")  # typed into the rename input, must not quit
        await pilot.pause()
        assert app.is_running
        await pilot.press("escape")
        await pilot.pause()
        assert store.sessions[SID1].label == ""


async def test_new_session_defaults_to_launch_cwd(world, tmp_path, monkeypatch):
    app, store, tmux, _ = world
    launch_dir = tmp_path / "launchhere"
    launch_dir.mkdir()
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(launch_dir))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # selection must NOT influence the default
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        dir_input = app.screen.query_one("#dir")
        assert dir_input.value == str(launch_dir)
        await pilot.press("enter")
        await pilot.pause(0.2)
        directory, args, sid = tmux.created[-1]
        assert directory == str(launch_dir)
        assert args[0] == "--session-id"
        assert sid in store.sessions


async def test_new_session_dir_tab_completion(world, tmp_path, monkeypatch):
    app, store, tmux, _ = world
    base = tmp_path / "complete"
    (base / "projects-here").mkdir(parents=True)
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        dir_input = app.screen.query_one("#dir")
        dir_input.value = str(base / "proj")
        await pilot.press("tab")
        await pilot.pause()
        assert dir_input.value == str(base / "projects-here") + "/"


async def test_track_modal_lists_untracked(world, claude_dir, now):
    app, store, tmux, _ = world
    sid_new = "44444444-4444-4444-4444-444444444444"
    TranscriptBuilder(sid_new, "/proj/gamma").ai_title("Gamma work").user("hi").write(
        claude_dir, mtime=now - 50
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause(0.2)
        from cagents.modals import TrackModal

        assert isinstance(app.screen, TrackModal)
        await pilot.press("enter")  # filter -> focuses list
        await pilot.pause()
        await pilot.press("enter")  # select the only candidate
        await pilot.pause(0.1)
        assert sid_new in store.sessions
        assert store.sessions[sid_new].project_dir == "/proj/gamma"


async def test_selection_survives_refresh(world):
    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        selected = app.selected_session_id
        app.refresh_data()
        await pilot.pause(0.2)
        assert app.selected_session_id == selected


async def test_help_modal(world):
    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        from cagents.modals import HelpModal

        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


async def test_empty_store_shows_hint(claude_dir, tmp_path):
    store = Store.load(tmp_path / "state.json")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        grouped = app.query_one("#grouped-list", SessionList)
        assert grouped.option_count == 1
        assert grouped.get_option_at_index(0).disabled


async def test_new_session_is_selected_and_previewed(world, tmp_path, monkeypatch):
    app, store, tmux, _ = world
    launch = tmp_path / "newproj"
    launch.mkdir()
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(launch))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # somewhere else first
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.3)  # spawn + refresh + pending highlight
        _, _, new_id = tmux.created[-1]
        # the list highlight moved to the new session -> it drives the preview
        assert app.selected_session_id == new_id
        listing = app.query_one(f"#{app.active_view_id}-list", SessionList)
        assert listing.highlighted_session_id == new_id
        # ...and it survives another refresh (highlight is real, not transient)
        app.refresh_data()
        await pilot.pause(0.2)
        assert app.selected_session_id == new_id


async def test_open_link_prompts_to_associate_pr(world):
    app, store, tmux, _ = world
    opened = []
    app._open_url = lambda url, label: opened.append(url)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # no links recorded
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        from cagents.modals import InputModal

        assert isinstance(app.screen, InputModal)  # prompted instead of warning
        await pilot.press(*"https://github.com/o/r/pull/5")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert store.sessions[SID1].pr_url == "https://github.com/o/r/pull/5"
        assert opened == ["https://github.com/o/r/pull/5"]  # opened right away
        # next time, o opens it directly — no prompt
        await pilot.press("o")
        await pilot.pause()
        assert not isinstance(app.screen, InputModal)
        assert opened[-1] == "https://github.com/o/r/pull/5"
