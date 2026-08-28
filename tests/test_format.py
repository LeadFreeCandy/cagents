"""Tests for the pure rendering helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import SID1, TranscriptBuilder

from cagents.claude_data import parse_session_file
from cagents.format import (
    TITLE_MAX,
    TITLE_MIN,
    RowWidths,
    header_summary,
    human_age,
    kanban_card,
    preview_renderable,
    row_widths,
    session_row,
)
from cagents.sessions import SessionState, SessionView
from cagents.store import TrackedSession


NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_human_age():
    assert human_age(None) == "?"
    assert human_age(NOW - timedelta(seconds=5), NOW) == "5s"
    assert human_age(NOW - timedelta(minutes=3), NOW) == "3m"
    assert human_age(NOW - timedelta(hours=7), NOW) == "7h"
    assert human_age(NOW - timedelta(days=2), NOW) == "2d"
    # Clock skew never renders negative ages
    assert human_age(NOW + timedelta(seconds=30), NOW) == "0s"


def _view(claude_dir: Path, state=SessionState.WORKING, label="") -> SessionView:
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.ai_title("Fix the login bug")
    b.user("fix it please", ts="2026-08-17T11:58:00.000Z")
    b.assistant_text("Working on it", ts="2026-08-17T11:59:00.000Z")
    parsed = parse_session_file(b.write(claude_dir))
    tracked = TrackedSession(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00", label=label)
    return SessionView(
        session_id=SID1,
        tracked=tracked,
        parsed=parsed,
        state=state,
        live=True,
        tmux_name="alpha",
        state_detail="thinking",
    )


def test_session_row_contains_essentials(claude_dir: Path):
    row = session_row(_view(claude_dir), NOW)
    text = row.plain
    assert "Fix the login bug" in text
    assert "working" in text
    assert "1m" in text


def test_session_row_shows_project(claude_dir: Path):
    row = session_row(_view(claude_dir), NOW, show_project=True)
    assert "alpha" in row.plain


def test_session_row_shows_state_detail(claude_dir: Path):
    row = session_row(_view(claude_dir), NOW)
    assert "thinking" in row.plain


def test_session_row_compact_omits_state_detail(claude_dir: Path):
    row = session_row(_view(claude_dir), NOW, compact=True)
    assert "thinking" not in row.plain


def test_label_overrides_title(claude_dir: Path):
    view = _view(claude_dir, label="my label")
    assert view.title == "my label"
    assert "my label" in session_row(view, NOW).plain


def test_row_widths_fit_the_widest_visible_row(claude_dir: Path):
    views = [
        _view(claude_dir, label="retreat"),
        _view(claude_dir, label="oep-sightlab-selection"),
        _view(claude_dir, state=SessionState.EXTERNAL_UPDATE),
    ]
    widths = row_widths(views)
    assert widths.title == len("oep-sightlab-selection")
    assert widths.state == len("external update")


def test_row_widths_are_bounded(claude_dir: Path):
    # One very long AI title must not shove every other column off the edge;
    # one tiny label must not squash the column to nothing.
    assert row_widths([_view(claude_dir, label="x" * 120)]).title == TITLE_MAX
    assert row_widths([_view(claude_dir, label="ab")]).title == TITLE_MIN
    assert row_widths([]) == RowWidths()


def test_session_rows_align_the_state_column_across_title_lengths(claude_dir: Path):
    short = _view(claude_dir, label="retreat")
    long = _view(claude_dir, label="oep-sightlab-selection")
    widths = row_widths([short, long])
    short_row, long_row = session_row(short, NOW, widths=widths), session_row(long, NOW, widths=widths)
    assert short_row.plain.index("working") == long_row.plain.index("working")
    # Sized to the visible titles: the widest one sits two spaces from the
    # state column, not a 44-column pad away.
    assert long_row.plain.index("working") == long_row.plain.index("oep-sightlab-selection") + widths.title + 2


def test_row_widths_default_keeps_the_original_layout(claude_dir: Path):
    row = session_row(_view(claude_dir), NOW)
    assert row.plain.index("working") == 3 + 44 + 2


def test_preview_shows_compaction_hint(claude_dir: Path):
    from rich.console import Console

    view = _view(claude_dir)
    view.parsed.compact_count = 2
    view.parsed.compacted_tokens = 1200000
    console = Console(width=100, record=True)
    console.print(preview_renderable(view, NOW))
    out = console.export_text()
    assert "compacted 2x" in out
    assert "1200k dropped" in out


def test_kanban_card(claude_dir: Path):
    card = kanban_card(_view(claude_dir), NOW)
    assert "Fix the login bug" in card.plain
    assert "alpha" in card.plain
    assert "thinking" in card.plain


def test_preview_renderable_shows_conversation(claude_dir: Path):
    from rich.console import Console

    console = Console(width=100, record=True)
    console.print(preview_renderable(_view(claude_dir), NOW))
    out = console.export_text()
    assert "Fix the login bug" in out
    assert "fix it please" in out
    assert "Working on it" in out
    assert "/proj/alpha" in out
    assert "tmux:alpha" in out


def test_preview_renderable_missing_transcript():
    tracked = TrackedSession(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    view = SessionView(
        session_id=SID1,
        tracked=tracked,
        parsed=None,
        state=SessionState.STOPPED,
        live=False,
        missing=True,
    )
    from rich.console import Console

    console = Console(width=80, record=True)
    console.print(preview_renderable(view, NOW))
    assert "not found" in console.export_text()


def test_header_summary_counts():
    counts = {
        SessionState.WORKING: 2,
        SessionState.NEEDS_INPUT: 1,
        SessionState.DONE: 3,
    }
    text = header_summary(counts).plain
    assert "6 sessions" in text
    assert "1 needs you" in text
    assert "2 working" in text
    assert "3 done" in text
    assert "review" not in text  # zero counts are hidden
