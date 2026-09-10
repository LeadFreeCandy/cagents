"""Refreshes and rapid navigation must not reset the list or attach intermediate rows."""
import asyncio

from rich.text import Text
from textual.app import App
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from cagents.app import CagentsApp, VIEWER_COALESCE
from cagents.store import Store
from cagents.views import SessionList
from conftest import FakeTmux


def rows(start=0, changed=False):
    return [Option(Text(f'Conversation {i}' + (' updated' if changed and i == 30 else '')), id=str(i))
            for i in range(start, 80)]


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
