"""Refreshes and rapid navigation must not reset the list or attach intermediate rows."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from rich.text import Text
from textual.app import App
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from cagents.app import CagentsApp, VIEWER_COALESCE
from cagents.store import Store
from cagents.views import GroupedView, QueueView, SessionList
from cagents.claude_data import ParsedSession
from cagents.sessions import SessionState, SessionView, Snapshot
from conftest import FakeTmux


def rows(start=0, changed=False):
    return [Option(Text(f'Conversation {i}' + (' updated' if changed and i == 30 else '')), id=str(i))
            for i in range(start, 80)]


@pytest.mark.parametrize("view_type", [QueueView, GroupedView])
@pytest.mark.parametrize("auto_done", [False, True])
async def test_compact_rows_fill_available_title_space_after_resizing(tmp_path, view_type, auto_done):
    class SidebarApp(App):
        compact = True

        def __init__(self):
            super().__init__()
            self.store = Store(tmp_path / "state.json")
            self.store.settings["conversation_title_width"] = 8

        def compose(self):
            yield view_type()

    app = SidebarApp()
    now = datetime.now(timezone.utc)
    views = []
    for i in range(40):
        sid = ("codex:" if i % 2 == 0 else "") + f"session-{i}"
        tracked = app.store.track(sid, "/proj", now.isoformat())
        parsed = ParsedSession(sid, tmp_path / "unused", cwd="/proj",
                               title=("東京 widget " if i % 2 else "Conversation ") * 10,
                               last_timestamp=now - timedelta(minutes=1))
        views.append(SessionView(sid, tracked, parsed, SessionState.DONE if auto_done else SessionState.NEEDS_REVIEW,
                                 False, auto_done=auto_done))
    async with app.run_test(size=(34, 10)) as pilot:
        view = app.query_one(view_type)
        view.update_snapshot(Snapshot(views=views))
        await pilot.pause()
        listing = app.query_one(SessionList)
        selected = listing.highlighted_session_id
        for width in (34, 50, 28, 34):
            await pilot.resize_terminal(width, 10)
            await pilot.pause()
            # Check actual rendered rows, including option padding and scrollbar,
            # rather than just the Rich Text before Textual clips it.
            first_row = 1 if view_type is GroupedView else 0
            for i in range(2):
                text = listing.render_line(first_row + i).text
                glyph = "✓" if auto_done else "◆"
                title = Text(views[i].title)
                title.truncate(listing.prompt_width - 3, overflow="ellipsis", pad=True)
                assert text.strip() == f"{glyph} {title.plain}".strip(), text
                assert text.count("…") == 1, text
                assert listing.get_option(views[i].session_id).prompt.cell_len == listing.prompt_width
            assert listing.highlighted_session_id == selected


async def test_refresh_keeps_option_identity_scroll_and_highlight():
    class ListApp(App):
        def compose(self):
            yield SessionList(*rows())

    app = ListApp()
    async with app.run_test(size=(70, 15)) as pilot:
        listing = app.query_one(SessionList)
        listing.highlighted = 30
        await pilot.pause()
        listing.scroll_to(y=25, animate=False)
        await pilot.pause()
        before, scroll = list(listing.options), listing.scroll_offset
        for _ in range(5):
            listing.rebuild(rows(changed=True), '30')
        await pilot.pause()
        assert list(listing.options) == before
        assert listing.get_option('30').prompt.plain.endswith('updated')
        assert listing.scroll_offset == scroll
        assert listing.highlighted_session_id == '30'

        events = []
        original = listing.post_message
        def record(event):
            posted = original(event)
            if posted and isinstance(event, OptionList.OptionHighlighted):
                events.append(event.option.id)
            return posted
        listing.post_message = record
        listing.rebuild(rows(start=5), '30')
        await pilot.pause()
        assert listing.highlighted_session_id == '30'
        assert listing.scroll_y > 0
        assert events == []  # no temporary first-row highlight during the rebuild


async def test_viewer_waits_for_the_last_keypress_in_a_burst(tmp_path, monkeypatch):
    app = CagentsApp(store=Store(tmp_path/'state.json'), tmux=FakeTmux(), claude_dir=tmp_path/'claude')
    monkeypatch.setattr(app, 'refresh_data', lambda: None)
    calls = []
    monkeypatch.setattr(app, '_sync_viewer', lambda: calls.append(app.selected_session_id))
    async with app.run_test() as pilot:
        app.sidecar = object()  # sync is stubbed; enable the scheduling path
        for sid in ['first', 'second', 'third']:
            app.selected_session_id = sid
            app._schedule_viewer_sync()
            assert calls == []
            await asyncio.sleep(VIEWER_COALESCE / 4)
        await asyncio.sleep(VIEWER_COALESCE)
        assert calls == ['third']
