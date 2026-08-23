"""The three ways of looking at your sessions.

- GroupedView: sessions grouped by project directory (the default).
- QueueView: one flat list ordered by who needs your attention first.
- KanbanView: columns by lifecycle state.

Each view renders from an immutable Snapshot and remembers its own
selection across refreshes, so a background refresh never moves the
cursor out from under you (spec §4.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .format import group_header, jira_header, kanban_card, session_row
from .sessions import SessionState, Snapshot, SessionView


class SessionList(OptionList):
    """OptionList with vim keys and stable-selection rebuilds."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G", "last", "Last", show=False),
    ]

    def rebuild(self, options: list[Option], keep_id: str | None) -> None:
        """Replace all options, restoring the highlight to `keep_id` (or the
        nearest selectable row) without flicker."""
        old_index = self.highlighted
        self.clear_options()
        if not options:
            return
        self.add_options(options)
        index = None
        if keep_id is not None:
            for i, opt in enumerate(options):
                if opt.id == keep_id:
                    index = i
                    break
        if index is None:
            # Keep roughly the same position in the list.
            candidates = [i for i, o in enumerate(options) if not o.disabled]
            if not candidates:
                return
            if old_index is None:
                index = candidates[0]
            else:
                index = min(candidates, key=lambda i: abs(i - old_index))
        self.highlighted = index

    @property
    def highlighted_session_id(self) -> str | None:
        if self.highlighted is None:
            return None
        try:
            option = self.get_option_at_index(self.highlighted)
        except Exception:
            return None
        return option.id


class SelectionChanged(Message):
    """A view's selected session changed (or was re-asserted)."""

    def __init__(self, view_id: str | None, session_id: str | None) -> None:
        self.view_id = view_id
        self.session_id = session_id
        super().__init__()


class BaseSessionView(Widget):
    """Common plumbing: track selection, re-emit as SelectionChanged."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.selected_id: str | None = None
        self.snapshot: Snapshot = Snapshot()

    def _emit_selection(self, session_id: str | None) -> None:
        self.selected_id = session_id
        self.post_message(SelectionChanged(self.id, session_id))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.disabled or event.option.id is None:
            return
        self._emit_selection(event.option.id)

    # Subclasses implement:
    def update_snapshot(self, snapshot: Snapshot) -> None:
        raise NotImplementedError

    def focus_list(self) -> None:
        raise NotImplementedError


def _empty_option() -> Option:
    return Option("  No sessions here — press 'a' to track one, 'n' to start one.", disabled=True)


class GroupedView(BaseSessionView):
    """Sessions grouped by project directory."""

    DEFAULT_CSS = """
    GroupedView { height: 1fr; }
    GroupedView > SessionList { height: 1fr; }
    GroupedView > .jira-header { height: 1; display: none; }
    GroupedView > .jira-header.shown { display: block; }
    """

    def compose(self):
        yield Static(id="grouped-jira-header", classes="jira-header")
        yield SessionList(id="grouped-list")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        now = datetime.now(timezone.utc)
        options: list[Option] = []
        groups: dict[str, list[SessionView]] = {}
        for view in snapshot.views:
            groups.setdefault(view.project_dir, []).append(view)
        compact = bool(getattr(self.app, "compact", False))
        show_jira = bool(self.app.store.get_setting("jira_integration")) and not compact
        header = self.query_one("#grouped-jira-header", Static)
        header.set_class(show_jira, "shown")
        if show_jira:
            header.update(jira_header())
        for project_dir in sorted(groups):
            views = groups[project_dir]
            options.append(
                Option(group_header(project_dir, len(views), compact=compact), disabled=True)
            )
            # Inside a group: most urgent first, then by rank_stable_since
            # (when the session last actually changed state) — NOT
            # last_activity, which ticks forward on every token while
            # genuinely WORKING and would otherwise reorder the list on
            # every refresh even though nothing meaningful changed.
            views.sort(key=lambda v: (v.attention_rank, -v.rank_stable_since))
            for view in views:
                options.append(
                    Option(session_row(view, now, compact=compact, show_jira=show_jira), id=view.session_id)
                )
        if not options:
            options = [_empty_option()]
        session_list = self.query_one("#grouped-list", SessionList)
        session_list.rebuild(options, self.selected_id)
        self._emit_selection(session_list.highlighted_session_id)

    def focus_list(self) -> None:
        self.query_one("#grouped-list", SessionList).focus()


class QueueView(BaseSessionView):
    """One flat list: whatever needs you most, first."""

    DEFAULT_CSS = """
    QueueView { height: 1fr; }
    QueueView > SessionList { height: 1fr; }
    QueueView > .jira-header { height: 1; display: none; }
    QueueView > .jira-header.shown { display: block; }
    """

    def compose(self):
        yield Static(id="queue-jira-header", classes="jira-header")
        yield SessionList(id="queue-list")

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        now = datetime.now(timezone.utc)
        # Stable within a rank — see the comment in GroupedView above.
        ordered = sorted(
            snapshot.views, key=lambda v: (v.attention_rank, -v.rank_stable_since)
        )
        compact = bool(getattr(self.app, "compact", False))
        show_jira = bool(self.app.store.get_setting("jira_integration")) and not compact
        header = self.query_one("#queue-jira-header", Static)
        header.set_class(show_jira, "shown")
        if show_jira:
            header.update(jira_header())
        options = [
            Option(
                session_row(view, now, show_project=not compact, compact=compact, show_jira=show_jira),
                id=view.session_id,
            )
            for view in ordered
        ]
        if not options:
            options = [_empty_option()]
        session_list = self.query_one("#queue-list", SessionList)
        session_list.rebuild(options, self.selected_id)
        self._emit_selection(session_list.highlighted_session_id)

    def focus_list(self) -> None:
        self.query_one("#queue-list", SessionList).focus()


# Kanban columns: (title, states shown, css id)
KANBAN_COLUMNS: list[tuple[str, tuple[SessionState, ...], str]] = [
    ("◉ Needs you", (SessionState.NEEDS_INPUT,), "kb-needs-you"),
    (
        "● Working",
        (SessionState.WORKING, SessionState.SHELL_RUNNING, SessionState.MONITORING, SessionState.BACKGROUND),
        "kb-working",
    ),
    (
        "◆ To review",
        (SessionState.NEEDS_REVIEW, SessionState.EXTERNAL_UPDATE, SessionState.WAITING_EXTERNAL),
        "kb-review",
    ),
    ("✓ Done / stopped", (SessionState.DONE, SessionState.STOPPED), "kb-done"),
]


class KanbanColumn(Widget):
    DEFAULT_CSS = """
    KanbanColumn {
        width: 1fr;
        height: 1fr;
        border: round $surface-lighten-2;
        border-title-align: center;
    }
    KanbanColumn > SessionList { height: 1fr; }
    KanbanColumn.has-items { border: round $primary-darken-1; }
    """

    def __init__(self, title: str, states: tuple[SessionState, ...], list_id: str) -> None:
        super().__init__()
        self.col_title = title
        self.states = states
        self.list_id = list_id
        self.border_title = title

    def compose(self):
        yield SessionList(id=self.list_id)


class KanbanView(BaseSessionView):
    """Columns by lifecycle state; h/l (or ←/→) move between columns."""

    DEFAULT_CSS = """
    KanbanView { height: 1fr; }
    KanbanView > Horizontal { height: 1fr; }
    """

    BINDINGS = [
        Binding("left", "prev_column", "Prev column", show=False),
        Binding("right", "next_column", "Next column", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.active_column = 0

    def compose(self):
        with Horizontal():
            for title, states, list_id in KANBAN_COLUMNS:
                yield KanbanColumn(title, states, list_id)

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        now = datetime.now(timezone.utc)
        for column in self.query(KanbanColumn):
            views = [v for v in snapshot.views if v.state in column.states]
            views.sort(key=lambda v: -v.rank_stable_since)  # stable — see GroupedView
            options = [Option(kanban_card(view, now), id=view.session_id) for view in views]
            session_list = column.query_one(SessionList)
            keep = self.selected_id if self.selected_id in {v.session_id for v in views} else None
            session_list.rebuild(options, keep)
            column.border_title = f"{column.col_title} ({len(views)})"
            column.set_class(bool(views), "has-items")
        self._emit_selection(self._current_selection())

    def _columns(self) -> list[KanbanColumn]:
        return list(self.query(KanbanColumn))

    def _current_selection(self) -> str | None:
        columns = self._columns()
        if not columns:
            return None
        column = columns[min(self.active_column, len(columns) - 1)]
        return column.query_one(SessionList).highlighted_session_id

    def focus_list(self) -> None:
        columns = self._columns()
        if not columns:
            return
        column = columns[min(self.active_column, len(columns) - 1)]
        column.query_one(SessionList).focus()
        self._emit_selection(self._current_selection())

    def _move_column(self, delta: int) -> None:
        columns = self._columns()
        if not columns:
            return
        n = len(columns)
        # Prefer the next non-empty column; fall back to plain move.
        i = self.active_column
        for _ in range(1, n + 1):
            i = (i + delta) % n
            if columns[i].query_one(SessionList).option_count > 0:
                break
        self.active_column = i
        self.focus_list()

    def action_prev_column(self) -> None:
        self._move_column(-1)

    def action_next_column(self) -> None:
        self._move_column(1)

    def on_descendant_focus(self, event) -> None:
        # Clicking into a column makes it the active one.
        columns = self._columns()
        for i, column in enumerate(columns):
            if column in event.widget.ancestors_with_self:
                if i != self.active_column:
                    self.active_column = i
                    self._emit_selection(self._current_selection())
                break
