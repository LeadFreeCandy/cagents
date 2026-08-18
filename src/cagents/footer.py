"""A priority-ordered footer.

Textual's built-in `Footer` renders every `show=True` binding and, when
they don't all fit, just silently clips whatever falls off the edge — no
control over *which* survive. cagents has opinions about that: the core
loop (attach, fork, mark done) must stay visible as long as anything is,
and less essential bindings degrade first as the terminal narrows.

Kept as pure functions (`visible_items`, `render_line`) plus a thin
Textual widget wrapper, so the layout logic is testable without spinning
up the TUI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import events
from textual.widgets import Static


@dataclass(frozen=True)
class FooterItem:
    key: str
    label: str
    setting: str | None = None  # hidden entirely while store.get_setting(setting) is falsy


# Priority order, highest first. Rendered left to right in this order for
# whichever prefix — well, whichever *subset*, see visible_items — fits.
FOOTER_PRIORITY: list[FooterItem] = [
    FooterItem("enter", "Attach"),
    FooterItem("F", "Fork"),
    FooterItem("r", "Done"),  # toggle_reviewed — this is what marks a session done
    FooterItem("D", "Diff"),
    FooterItem("n", "New"),
    FooterItem("2", "Queue"),
    FooterItem("1", "Grouped"),
    FooterItem("4", "Todos", setting="todos_enabled"),
    FooterItem("a", "Track"),
    FooterItem("H", "Handoff"),
    FooterItem("?", "Help"),
    FooterItem("q", "Quit"),
    FooterItem("3", "Kanban"),
    FooterItem("*", "Related"),
    FooterItem("t", "Shell"),
    FooterItem("V", "Rich diff"),
    FooterItem("m", "Monitor"),
    FooterItem(",", "Settings"),
    FooterItem(":", "Fleet"),
    FooterItem("o", "Open link"),
    FooterItem("e", "Note"),
    FooterItem("L", "Label"),
    FooterItem("x", "Untrack"),
    FooterItem("=", "Expand"),
    FooterItem("R", "Refresh"),
]

_GAP = 2  # spacing rendered between "key label" pairs


def _item_width(item: FooterItem) -> int:
    return len(item.key) + 1 + len(item.label)  # "key label"


def visible_items(
    width: int, items: list[FooterItem] = FOOTER_PRIORITY, setting_enabled: Callable[[str], bool] = lambda _: True,
) -> list[FooterItem]:
    """Greedily fill `width` in priority order: try each candidate in turn,
    skip (don't stop at) ones that don't currently fit — a short low-
    priority item after a skipped long one still gets a chance."""
    chosen: list[FooterItem] = []
    used = 0
    for item in items:
        if item.setting and not setting_enabled(item.setting):
            continue
        w = _item_width(item) + (_GAP if chosen else 0)
        if used + w > max(0, width):
            continue
        chosen.append(item)
        used += w
    return chosen


def render_line(items: list[FooterItem]) -> Text:
    line = Text()
    for i, item in enumerate(items):
        if i:
            line.append(" " * _GAP)
        line.append(f" {item.key} ", style="bold black on bright_yellow")
        line.append(f" {item.label}", style="")
    return line


class PriorityFooter(Static):
    """Drop-in replacement for `Footer()` — same dock/height, different
    (priority-aware, width-fitting) fill logic."""

    DEFAULT_CSS = """
    PriorityFooter {
        dock: bottom;
        height: 1;
        background: $footer-background;
        color: $footer-foreground;
    }
    """

    def __init__(self, setting_enabled: Callable[[str], bool] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._setting_enabled = setting_enabled or (lambda _: True)

    def on_mount(self) -> None:
        self.refresh_items()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_items()

    def refresh_items(self) -> None:
        items = visible_items(self.size.width, setting_enabled=self._setting_enabled)
        self.update(render_line(items))
