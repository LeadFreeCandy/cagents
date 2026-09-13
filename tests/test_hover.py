"""Hover may wake a conversation, but only click/keyboard may select it."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from cagents.app import CagentsApp, VIEWER_COALESCE
from cagents.claude_data import ParsedSession
from cagents.sessions import SessionState, SessionView, Snapshot
from cagents.store import Store
from cagents.views import KanbanView, SessionList
from conftest import FakeTmux


def hover_app(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude")
    monkeypatch.setattr(app, "refresh_data", lambda: None)
    now = datetime.now(timezone.utc)
    views = []
    for sid in ("claude-first", "codex:second"):
        tracked = store.track(sid, str(tmp_path), now.isoformat())
        parsed = ParsedSession(sid, tmp_path / "unused", cwd=str(tmp_path),
                               title=sid, last_timestamp=now)
        views.append(SessionView(sid, tracked, parsed, SessionState.NEEDS_REVIEW,
                                 live=True, tmux_name=sid))
    app.snapshot = Snapshot(views=views)
    return app, views


def row_offset(listing, sid):
    """Find the actual rendered row, including headers and multi-line cards."""
    index = listing.get_option_index(sid)
    region = listing.content_region
    x = region.x + 2
    for y in range(region.y, region.bottom):
        if listing.screen.get_style_at(x, y).meta.get("option") == index:
            return (x - listing.region.x, y - listing.region.y)
    raise AssertionError(f"{sid} was not rendered in {listing.id}")


@pytest.mark.parametrize("view_id,list_id", [
    ("queue", "queue-list"), ("grouped", "grouped-list"), ("kanban", "kb-review"),
])
async def test_hover_keeps_selection_and_viewer_until_click_or_keyboard(tmp_path, monkeypatch, view_id, list_id):
    app, views = hover_app(tmp_path, monkeypatch)
    shown = []
    monkeypatch.setattr(app, "_sync_viewer", lambda: shown.append(app.selected_session_id))
    async with app.run_test(size=(160, 40)) as pilot:
        if view_id == "kanban":
            app.query_one(KanbanView).active_column = 3
        app.action_switch_view(view_id)
        await pilot.pause()
        listing = app.query_one(f"#{list_id}", SessionList)
        first, second = [view.session_id for view in views]
        assert app.selected_session_id == first
        app.sidecar = Mock()  # keep viewer scheduling active without a real tmux server
        app._viewer_target = "visible-first"
        offset = row_offset(listing, second)
        await pilot.hover(listing, offset=offset)
        await pilot.pause(VIEWER_COALESCE * 2)
        assert listing.highlighted_session_id == first
        assert app.current_view().selected_id == app.selected_session_id == first
        assert app._viewer_target == "visible-first"
        assert shown == []
        app.sidecar.show_viewer.assert_not_called()
        assert views[1].tracked.last_interacted_at  # hover still records activity/wakes its own row
        assert not views[0].tracked.last_interacted_at

        await pilot.click(listing, offset=offset)
        await pilot.pause(VIEWER_COALESCE * 2)
        assert listing.highlighted_session_id == app.selected_session_id == second
        # Click attaches immediately; keyboard navigation uses the debounce.
        app.sidecar.show_viewer.assert_called_once_with(app._viewer_command(views[1]))
        assert app._viewer_target == app._viewer_command(views[1])
        await pilot.press("up")
        await pilot.pause(VIEWER_COALESCE * 2)
        assert listing.highlighted_session_id == app.selected_session_id == first
        assert shown == [first]


@pytest.mark.parametrize("selected", [False, True])
def test_hover_resumes_stopped_done_without_showing_an_unselected_conversation(tmp_path, monkeypatch, selected):
    app, (first, done) = hover_app(tmp_path, monkeypatch)
    done.live, done.state = False, SessionState.DONE
    app.selected_session_id = done.session_id if selected else first.session_id
    app.sidecar = Mock()
    app._viewer_target = "visible-first"
    resume = Mock(return_value=("resumed-agent", "", ""))
    monkeypatch.setattr(app, "_resume_target", resume)
    app._interact_session(done.session_id)
    resume.assert_called_once_with(done)
    assert done.tracked.last_interacted_at
    assert app.selected_session_id == (done.session_id if selected else first.session_id)
    assert app.sidecar.show_viewer.call_count == int(selected)
    if not selected:
        assert app._viewer_target == "visible-first"


@pytest.mark.parametrize("selected", [False, True])
def test_background_hover_resume_does_not_reattach_the_visible_pane(tmp_path, monkeypatch, selected):
    app, (first, suspended) = hover_app(tmp_path, monkeypatch)
    suspended.live, suspended.suspended = False, True
    app.selected_session_id = suspended.session_id if selected else first.session_id
    app._viewer_target = "visible-first"
    sync = Mock()
    monkeypatch.setattr(app, "_schedule_viewer_sync", sync)
    app._lifecycle_finished(suspended, "resume", False, "")
    assert suspended.live and not suspended.suspended
    assert app._viewer_target == ("" if selected else "visible-first")
    assert sync.call_count == int(selected)


async def test_wake_on_browse_off_only_click_or_enter_starts_a_row(tmp_path, monkeypatch):
    """With wake_on_browse off, passing over a row — pointer or arrow keys —
    must never start it; only a click or Enter may. Default stays on (hover
    pre-warms), so this is opt-in for people with long lists."""
    app, (first, done) = hover_app(tmp_path, monkeypatch)
    app.store.set_setting("wake_on_browse", False)
    done.live, done.state = False, SessionState.DONE
    resume = Mock(return_value=("resumed-agent", "", ""))
    monkeypatch.setattr(app, "_resume_target", resume)
    async with app.run_test(size=(160, 40)) as pilot:
        app.action_switch_view("queue")
        await pilot.pause()
        listing = app.query_one("#queue-list", SessionList)
        app.sidecar = Mock()
        offset = row_offset(listing, done.session_id)
        await pilot.hover(listing, offset=offset)
        await pilot.pause(0.3)
        assert resume.call_count == 0, "hover started it"
        await pilot.press("down")  # arrow onto it
        await pilot.pause(0.3)
        assert listing.highlighted_session_id == done.session_id
        assert resume.call_count == 0, "arrowing onto it started it"
        # Inert means inert: passing over a row must not reset its idle
        # clock either, or a pointer sweep defers every row's suspension.
        assert not done.tracked.last_interacted_at
        await pilot.click(listing, offset=offset)
        await pilot.pause(0.3)
        assert resume.call_count == 1, "click must start it"
        assert done.tracked.last_interacted_at  # a click is a real touch


async def test_wake_on_browse_off_enter_still_resumes(tmp_path, monkeypatch):
    app, (first, done) = hover_app(tmp_path, monkeypatch)
    app.store.set_setting("wake_on_browse", False)
    done.live, done.state = False, SessionState.DONE
    resume = Mock(return_value=("resumed-agent", "", ""))
    monkeypatch.setattr(app, "_resume_target", resume)
    monkeypatch.setattr(app, "_show_new_session", Mock())
    async with app.run_test(size=(160, 40)) as pilot:
        app.action_switch_view("queue")
        await pilot.pause()
        listing = app.query_one("#queue-list", SessionList)
        app.sidecar = Mock()
        await pilot.press("down")
        await pilot.pause(0.3)
        assert listing.highlighted_session_id == done.session_id and resume.call_count == 0
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert resume.call_count == 1


async def test_wake_on_browse_off_dead_row_pane_says_press_enter(tmp_path, monkeypatch):
    """Selecting a dead row with the setting off must not quietly resume it
    via the viewer sync either; the pane says what to do instead."""
    app, (first, dead) = hover_app(tmp_path, monkeypatch)
    app.store.set_setting("wake_on_browse", False)
    dead.live, dead.state = False, SessionState.NEEDS_REVIEW
    resume = Mock(return_value=("resumed-agent", "", ""))
    monkeypatch.setattr(app, "_resume_target", resume)
    async with app.run_test(size=(160, 40)) as pilot:
        app.action_switch_view("queue")
        await pilot.pause()
        app.sidecar = Mock()
        await pilot.press("down")
        await pilot.pause(VIEWER_COALESCE * 3)
        assert resume.call_count == 0
        shown = [c.args[0] for c in app.sidecar.show_viewer.call_args_list]
        assert shown and "Enter" in shown[-1], shown


async def test_click_on_a_dead_done_row_resumes_it_exactly_once(tmp_path, monkeypatch):
    """A click both selects (OptionSelected → attach, which resumes) and
    posts an interaction; the interaction path must not resume a second
    CLI onto the same transcript. Default settings — this is independent
    of wake_on_browse."""
    app, (first, done) = hover_app(tmp_path, monkeypatch)
    done.live, done.state = False, SessionState.DONE
    resume = Mock(return_value=("resumed-agent", "", ""))
    monkeypatch.setattr(app, "_resume_target", resume)
    monkeypatch.setattr(app, "_show_new_session", Mock())
    async with app.run_test(size=(160, 40)) as pilot:
        app.action_switch_view("queue")
        await pilot.pause()
        listing = app.query_one("#queue-list", SessionList)
        app.sidecar = Mock()
        await pilot.click(listing, offset=row_offset(listing, done.session_id))
        await pilot.pause(0.3)
        assert resume.call_count == 1


@pytest.mark.parametrize("wake_on_browse", [False, True])
async def test_right_arrow_enters_a_conversation_like_enter(tmp_path, monkeypatch, wake_on_browse):
    """→ walks into the session pane, so it is an explicit way in — like
    Enter and a click, it must start a dormant conversation (once, whatever
    the setting) and count as touching it. It used to only move focus."""
    app, (first, done) = hover_app(tmp_path, monkeypatch)
    app.store.set_setting("wake_on_browse", wake_on_browse)
    done.live, done.state = False, SessionState.DONE
    resume = Mock(return_value=("resumed-agent", "", ""))
    monkeypatch.setattr(app, "_resume_target", resume)
    monkeypatch.setattr(app, "_show_new_session", Mock())
    async with app.run_test(size=(160, 40)) as pilot:
        app.action_switch_view("queue")
        await pilot.pause()
        listing = app.query_one("#queue-list", SessionList)
        app.sidecar = Mock()
        # land on the row; whatever the passive visit did, count from here
        await pilot.press("down")
        await pilot.pause(0.3)
        assert listing.highlighted_session_id == done.session_id
        before = resume.call_count
        done.tracked.last_interacted_at = ""
        app._resumed_for_preview.clear()
        await pilot.press("right")
        await pilot.pause(0.3)
        assert resume.call_count == before + 1, "→ must start it exactly once"
        assert done.tracked.last_interacted_at, "→ is a real touch"
