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
    SessionState.MONITORING: ("◎", "bold cyan", "monitoring"),
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


def session_row(
    view: SessionView,
    now: datetime | None = None,
    show_project: bool = False,
    compact: bool = False,
) -> Text:
    """One list row: glyph, title, state, age (and optionally the project).
    Compact form (sidecar rail): glyph, short title, age — nothing else."""
    glyph, style, label = STATE_STYLE[view.state]
    if compact:
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append(f" {glyph} ", style=style)
        row.append(f"{_truncate(view.title, 22):<22} ", style="bold" if view.live else "")
        row.append(f"{human_age(view.last_activity, now):>3}", style="dim")
        return row
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
    if view.parsed and view.parsed.links:
        row.append("  ⇗", style="dim blue")  # PR / artifact recorded
    if view.parsed and view.parsed.pending_agents:
        row.append(f"  ⑂{view.parsed.pending_agents}", style="dim green")
    if view.parent_id:
        row.append("  ↳", style="dim magenta")  # forked/handed-off child
    if view.child_ids:
        row.append(f"  »{len(view.child_ids)}", style="dim magenta")  # has children
    return row


def group_header(project_dir: str, count: int, compact: bool = False) -> Text:
    header = Text(no_wrap=True, overflow="ellipsis")
    name = project_dir.rsplit("/", 1)[-1] or project_dir
    header.append("▍", style="bold blue")
    header.append(f"{name} ", style="bold")
    if not compact:
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
        if view.parsed.pending_agents:
            meta.append(f" · ⑂ {view.parsed.pending_agents} agents", style="green")
    if view.live:
        meta.append(f" · tmux:{view.tmux_name}", style="green")
    head.append_text(meta)
    parts.append(head)

    if view.parsed and view.parsed.links:
        links = Text()
        for i, link in enumerate(view.parsed.links[-4:]):
            if i:
                links.append("   ")
            links.append("⇗ ", style="blue")
            links.append(f"{link.label} ", style="bold blue")
            links.append(_truncate(link.url, 60), style="dim")
        links.append("   (o opens newest)", style="dim italic")
        parts.append(links)

    if view.parsed and view.parsed.files_touched:
        files = view.parsed.files_touched
        shown = files[-8:]
        try:
            import os

            common = os.path.commonpath(shown) if len(shown) > 1 else ""
        except ValueError:
            common = ""
        touched = Text()
        touched.append(f"Δ {len(files)} file{'s' if len(files) != 1 else ''}  ", style="yellow")
        names = [f[len(common) :].lstrip("/") if common and f.startswith(common) else f for f in shown]
        touched.append(_truncate(" · ".join(names), width * 2), style="dim")
        parts.append(touched)

    if view.parent_id or view.child_ids or view.sibling_ids:
        lineage = Text()
        lineage.append("↳ ", style="magenta")
        bits = []
        if view.parent_id:
            bits.append(f"{view.relation or 'child'} of {view.parent_id[:8]}")
        if view.child_ids:
            bits.append(f"{len(view.child_ids)} child{'ren' if len(view.child_ids) != 1 else ''}")
        if view.sibling_ids:
            bits.append(f"{len(view.sibling_ids)} sibling{'s' if len(view.sibling_ids) != 1 else ''}")
        lineage.append(" · ".join(bits), style="magenta")
        lineage.append("   (* to visit)", style="dim italic")
        parts.append(lineage)

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
        SessionState.MONITORING,
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
