"""End-to-end UI tests driven through Textual's pilot.

A fake tmux client and a temp Claude store give the app a fully
controlled world; no real tmux server or ~/.claude is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SID1, SID2, SID3, TranscriptBuilder

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry, SessionState
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from cagents.views import GroupedView, KanbanView, QueueView, SessionList


def widget_text(app, selector: str) -> str:
    """Plain text currently shown by a Static widget."""
    return render_text(app.query_one(selector).content)


def render_text(content) -> str:
    if hasattr(content, "plain"):
        return content.plain
    import io

    from rich.console import Console

    buffer = io.StringIO()
    console = Console(width=200, file=buffer, force_terminal=False)
    console.print(content)
    return buffer.getvalue()


def select_session(app, session_id: str) -> None:
    """Select a session the way a user would: by moving the list highlight.

    (Assigning app.selected_session_id directly is not enough — the next
    refresh re-asserts the list's real highlight, by design.)
    """
    session_list = app.query_one(f"#{app.active_view_id}-list", SessionList)
    for i in range(session_list.option_count):
        if session_list.get_option_at_index(i).id == session_id:
            session_list.highlighted = i
            return
    raise AssertionError(f"session {session_id} not in {app.active_view_id} list")


class FakeTmux:
    socket = "claude"

    def __init__(self):
        self.sessions: list[TmuxSession] = []
        self.panes: dict[str, str] = {}
        self.attached_to: list[str] = []
        self.created: list[tuple[str, list[str], str]] = []

    def available(self) -> bool:
        return True

    def list_sessions(self):
        return self.sessions

    def capture_pane(self, name: str, lines: int = 40) -> str:
        return self.panes.get(name, "")

    def attach(self, name: str) -> int:
        self.attached_to.append(name)
        return 0

    def new_claude_session(self, directory, claude_args, session_id="", claude_bin=""):
        name = Path(directory).name or "session"
        self.created.append((directory, claude_args, session_id))
        self.sessions.append(
            TmuxSession(
                name=name,
                created=1e12,
                activity=1e12,
                attached=False,
                pane_pid=1,
                pane_path=directory,
                cagents_session_id=session_id,
            )
        )
        return name


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    """Three sessions in two projects, one live in tmux."""
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user("go").assistant_text(
        "done with auth"
    ).write(claude_dir, mtime=now - 900)
    TranscriptBuilder(SID2, "/proj/alpha").ai_title("Alpha: add tests").user("go").assistant_tool_use(
        "t1", "Bash", {"command": "pytest"}
    ).write(claude_dir, mtime=now - 2)
    TranscriptBuilder(SID3, "/proj/beta").ai_title("Beta: refactor").user("go").assistant_text(
        "refactored"
    ).write(claude_dir, mtime=now - 4000)

    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.track(SID2, "/proj/alpha", "2026-08-17T09:10:00+00:00")
    store.track(SID3, "/proj/beta", "2026-08-17T08:00:00+00:00")

    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(
            name="alpha",
            created=now - 300,
            activity=now,
            attached=False,
            pane_pid=42,
            pane_path="/proj/alpha",
        )
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux, claude_dir


async def test_startup_shows_grouped_sessions(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.snapshot.views and len(app.snapshot.views) == 3
        # Live one is WORKING (fresh writes), others finished -> NEEDS_REVIEW
        by_id = {v.session_id: v for v in app.snapshot.views}
        assert by_id[SID2].state == SessionState.WORKING
        assert by_id[SID2].live is True
        assert by_id[SID1].state == SessionState.NEEDS_REVIEW
        assert by_id[SID3].state == SessionState.NEEDS_REVIEW

        # Grouped list: 2 group headers + 3 rows
        grouped = app.query_one("#grouped-list", SessionList)
        assert grouped.option_count == 5
        # Summary line shows counts
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
        assert widget_text(app, "#preview-content")  # preview renders the selection


async def test_view_switching(world):
    app, *_ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.active_view_id == "grouped"
        await pilot.press("2")
        await pilot.pause()
        assert app.active_view_id == "queue"
        queue = app.query_one("#queue-list", SessionList)
        assert queue.option_count == 3
        await pilot.press("3")
        await pilot.pause()
        assert app.active_view_id == "kanban"
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_view_id == "todos"
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_view_id == "grouped"


async def test_queue_orders_by_attention(world):
    app, store, tmux, claude_dir = world
    tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        queue = app.query_one("#queue-list", SessionList)
        # SID2 (needs input via pane prompt) must be first
        assert queue.get_option_at_index(0).id == SID2
        by_id = {v.session_id: v for v in app.snapshot.views}
        assert by_id[SID2].state == SessionState.NEEDS_INPUT


async def test_kanban_columns_populated(world):
    app, *_ = world
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        kanban = app.query_one("#kanban", KanbanView)
        working = app.query_one("#kb-working", SessionList)
        review = app.query_one("#kb-review", SessionList)
        needs_you = app.query_one("#kb-needs-you", SessionList)
        assert working.option_count == 1
        assert review.option_count == 2
        assert needs_you.option_count == 0


async def test_attach_live_session(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)  # the live one
        await pilot.pause()
        app.action_attach()
        await pilot.pause()
        assert tmux.attached_to == ["alpha"]


async def test_attach_dead_session_resumes_in_tmux(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID3)
        await pilot.pause()
        # /proj/beta doesn't exist on disk -> loud failure, no attach
        app.action_attach()
        await pilot.pause()
        assert tmux.attached_to == []

        # Point the project somewhere real and retry (mutate the *current*
        # snapshot: a background refresh may have replaced the old one)
        store.sessions[SID3].project_dir = "/tmp"
        view = app.snapshot.by_id(SID3)
        if view.parsed:
            view.parsed.cwd = "/tmp"
        app.action_attach()
        await pilot.pause()
        assert tmux.created and tmux.created[-1][1] == ["--resume", SID3]
        assert tmux.attached_to  # attached to the fresh tmux session


async def test_mark_reviewed_flow(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # needs review
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at != ""
        view = app.snapshot.by_id(SID1)
        assert view.state == SessionState.DONE

        # Toggle back
        await pilot.press("r")
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at == ""
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW


async def test_reviewing_working_session_refuses(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)  # working
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause(0.1)
        assert store.sessions[SID2].reviewed_at == ""


async def test_note_editing(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        await pilot.press(*"waiting on CI")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert store.sessions[SID1].note == "waiting on CI"


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
        await pilot.press("y")  # confirm
        await pilot.pause(0.1)
        assert SID3 not in store.sessions
        assert len(app.snapshot.views) == 2


async def test_app_keys_do_not_fire_inside_modal(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("e")  # open note modal
        await pilot.pause()
        # 'q' typed into the note input must not quit the app
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running
        await pilot.press("escape")
        await pilot.pause()
        assert store.sessions[SID1].note == ""


async def test_new_session_modal_creates_and_tracks(world, tmp_path):
    app, store, tmux, _ = world
    project = tmp_path / "newproj"
    project.mkdir()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        dir_input = app.screen.query_one("#dir")
        dir_input.value = str(project)
        await pilot.press("enter")
        await pilot.pause(0.1)
        # A new session was created in tmux with a generated session id...
        assert tmux.created
        directory, args, sid = tmux.created[-1]
        assert directory == str(project)
        assert args[0] == "--session-id"
        # ...tracked in the store, and attached
        assert sid in store.sessions
        assert tmux.attached_to


async def test_track_modal_lists_untracked(world, claude_dir, now):
    app, store, tmux, _ = world
    sid_new = "44444444-4444-4444-4444-444444444444"
    TranscriptBuilder(sid_new, "/proj/gamma").ai_title("Gamma work").user("hi").write(
        claude_dir, mtime=now - 50
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause(0.2)  # worker loads candidates
        from cagents.modals import TrackModal

        assert isinstance(app.screen, TrackModal)
        await pilot.press("enter")  # filter -> focuses list
        await pilot.pause()
        await pilot.press("enter")  # select the only candidate
        await pilot.pause(0.1)
        assert sid_new in store.sessions
        assert store.sessions[sid_new].project_dir == "/proj/gamma"


async def test_selection_survives_refresh(world):
    app, store, tmux, _ = world
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
