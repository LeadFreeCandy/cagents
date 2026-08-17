"""Pure rendering helpers: session data -> Rich renderables.

No Textual imports here; everything is testable without a running app.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.text import Text

from .sessions import SessionState, SessionView

# state -> (glyph, style, short label)
STATE_STYLE: dict[SessionState, tuple[str, str, str]] = {
    SessionState.WORKING: ("●", "bold green", "working"),
    SessionState.NEEDS_INPUT: ("◉", "bold red", "needs you"),
    SessionState.NEEDS_REVIEW: ("◆", "bold yellow", "review"),
    SessionState.DONE: ("✓", "bright_blue", "done"),
    SessionState.STOPPED: ("■", "dim", "stopped"),
}

PREVIEW_KIND_STYLE = {
    "user": ("you", "bold cyan"),
    "assistant": ("claude", "bold magenta"),
    "thinking": ("…", "dim italic"),
    "tool": ("⚙", "yellow"),
}


def human_age(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "?"
    now = now or datetime.now(timezone.utc)
    seconds = max(0.0, (now - when).total_seconds())
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def state_badge(view: SessionView) -> Text:
    glyph, style, label = STATE_STYLE[view.state]
    badge = Text()
    badge.append(glyph + " ", style=style)
    badge.append(label, style=style)
    return badge


def session_row(view: SessionView, now: datetime | None = None, show_project: bool = False) -> Text:
    """One list row: glyph, title, state, age (and optionally the project)."""
    glyph, style, label = STATE_STYLE[view.state]
    row = Text(no_wrap=True, overflow="ellipsis")
    row.append(f" {glyph} ", style=style)
    row.append(f"{_truncate(view.title, 44):<44}  ", style="bold" if view.live else "")
    row.append(f"{label:<10}", style=style)
    row.append(f"{human_age(view.last_activity, now):>4} ", style="dim")
    if show_project:
        row.append(f" {view.project_name}", style="dim cyan")
    if view.attached:
        row.append("  ⇄", style="dim")  # someone is attached right now
    if view.tracked.note:
        row.append("  ✎", style="dim yellow")
    return row


def group_header(project_dir: str, count: int) -> Text:
    header = Text(no_wrap=True, overflow="ellipsis")
    name = project_dir.rsplit("/", 1)[-1] or project_dir
    header.append("▍", style="bold blue")
    header.append(f"{name} ", style="bold")
    header.append(f"({project_dir}) ", style="dim")
    header.append(f"· {count}", style="dim")
    return header


def kanban_card(view: SessionView, now: datetime | None = None) -> Text:
    glyph, style, _ = STATE_STYLE[view.state]
    card = Text()
    card.append(f"{glyph} ", style=style)
    card.append(_truncate(view.title, 60), style="bold")
    card.append("\n  ")
    card.append(view.project_name, style="dim cyan")
    card.append(f" · {human_age(view.last_activity, now)}", style="dim")
    if view.state_detail:
        card.append("\n  ")
        card.append(_truncate(view.state_detail, 58), style="italic dim")
    return card


def preview_renderable(view: SessionView, now: datetime | None = None, width: int = 80) -> RenderableType:
    """The detail pane: session facts, then the real conversation tail."""
    parts: list[RenderableType] = []

    head = Text()
    head.append(_truncate(view.title, width - 2) + "\n", style="bold")
    head.append_text(state_badge(view))
    if view.state_detail:
        head.append(f" — {view.state_detail}", style="italic")
    head.append("\n")
    head.append(f"{view.project_dir}\n", style="dim cyan")

    meta = Text(style="dim")
    if view.parsed:
        if view.parsed.git_branch:
            meta.append(f" {view.parsed.git_branch} ")
        if view.parsed.model:
            meta.append(f"· {view.parsed.model} ")
        if view.started:
            meta.append(f"· started {human_age(view.started, now)} ago ")
        meta.append(f"· active {human_age(view.last_activity, now)} ago")
    if view.live:
        meta.append(f" · tmux:{view.tmux_name}", style="green")
    head.append_text(meta)
    parts.append(head)

    if view.tracked.note:
        note = Text()
        note.append("✎ ", style="yellow")
        note.append(view.tracked.note, style="yellow")
        parts.append(note)

    parts.append(Text("─" * max(10, width - 2), style="dim"))

    if view.missing:
        parts.append(Text("Transcript not found in Claude's store.", style="dim red"))
        return Group(*parts)
    if not view.parsed or not view.parsed.preview:
        parts.append(Text("No conversation yet.", style="dim"))
        return Group(*parts)

    for item in view.parsed.preview:
        prefix, style = PREVIEW_KIND_STYLE.get(item.kind, ("?", "dim"))
        line = Text()
        if item.kind == "tool":
            line.append(f"{prefix} {item.tool_name}", style=style)
            if item.text:
                line.append(f"  {_truncate(item.text, width)}", style="dim")
        elif item.kind == "thinking":
            line.append(f"{prefix} {_truncate(item.text, width * 2)}", style=style)
        else:
            line.append(f"{prefix}\n", style=style)
            line.append(item.text.strip())
        parts.append(line)
        parts.append(Text(""))

    return Group(*parts)


def header_summary(counts: dict[SessionState, int]) -> Text:
    """The one-line status summary in the app header."""
    text = Text()
    total = sum(counts.values())
    text.append(f" {total} session{'s' if total != 1 else ''}", style="bold")
    for state in (
        SessionState.NEEDS_INPUT,
        SessionState.NEEDS_REVIEW,
        SessionState.WORKING,
        SessionState.DONE,
        SessionState.STOPPED,
    ):
        n = counts.get(state, 0)
        if n:
            glyph, style, label = STATE_STYLE[state]
            text.append("  ·  ", style="dim")
            text.append(f"{glyph} {n} {label}", style=style)
    return text
