"""Peek mode: read a session's transcript in full screen without attaching.

This deepens core-loop step 3 (see what's happening without attaching):
the side preview shows the tail; peek shows as much history as a bounded
read allows, scrollable, with review one keystroke away. It never touches
the live session — it's the same read-only parse, just bigger.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .claude_data import parse_session_file
from .format import preview_renderable, state_badge
from .sessions import SessionView

# Peek reads far deeper than the side preview.
PEEK_TAIL_BYTES = 4 * 1024 * 1024
PEEK_ITEMS = 500


def deep_view(view: SessionView) -> SessionView:
    """Re-parse the session with a much larger window. Falls back to the
    already-parsed data if the file has gone missing."""
    if view.parsed is None:
        return view
    try:
        deep = parse_session_file(
            view.parsed.path, tail_bytes=PEEK_TAIL_BYTES, preview_items=PEEK_ITEMS
        )
    except OSError:
        return view
    return replace(view, parsed=deep)


class PeekScreen(ModalScreen[str | None]):
    """Dismisses with "reviewed" if the user marked the session reviewed."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("space", "close", "Close"),
        Binding("j", "scroll_down_line", "Down", show=False),
        Binding("k", "scroll_up_line", "Up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
        Binding("r", "mark_reviewed", "Reviewed"),
    ]

    DEFAULT_CSS = """
    PeekScreen { align: center middle; }
    PeekScreen > Vertical {
        width: 90%; height: 90%;
        border: round $primary; background: $surface;
    }
    PeekScreen #peek-title { height: 1; padding: 0 1; background: $panel; }
    PeekScreen #peek-scroll { height: 1fr; padding: 0 2; }
    PeekScreen #peek-hint { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, view: SessionView) -> None:
        super().__init__()
        self.view = view

    def compose(self) -> ComposeResult:
        from rich.text import Text

        title = Text()
        title.append_text(state_badge(self.view))
        title.append("  ")
        title.append(self.view.title, style="bold")
        title.append(f"   {self.view.project_dir}", style="dim cyan")
        if self.view.parsed and self.view.parsed.truncated:
            title.append("   (older history not shown)", style="dim italic")
        with Vertical():
            yield Static(title, id="peek-title")
            with VerticalScroll(id="peek-scroll"):
                yield Static(id="peek-body")
            yield Static(
                "j/k scroll · g/G top/bottom · r mark reviewed · esc close",
                id="peek-hint",
            )

    def on_mount(self) -> None:
        body = self.query_one("#peek-body", Static)
        width = max(60, self.app.size.width - 12)
        body.update(preview_renderable(self.view, datetime.now(timezone.utc), width=width))
        scroll = self.query_one("#peek-scroll", VerticalScroll)
        scroll.focus()
        self.call_after_refresh(lambda: scroll.scroll_end(animate=False))

    def _scroll(self) -> VerticalScroll:
        return self.query_one("#peek-scroll", VerticalScroll)

    def action_scroll_down_line(self) -> None:
        self._scroll().scroll_relative(y=2, animate=False)

    def action_scroll_up_line(self) -> None:
        self._scroll().scroll_relative(y=-2, animate=False)

    def action_scroll_home(self) -> None:
        self._scroll().scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        self._scroll().scroll_end(animate=False)

    def action_mark_reviewed(self) -> None:
        self.dismiss("reviewed")

    def action_close(self) -> None:
        self.dismiss(None)
