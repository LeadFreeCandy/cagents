"""The three ways of looking at your sessions.

- GroupedView: sessions grouped by project directory (the default).
- QueueView: one flat list ordered by who needs your attention first.
- KanbanView: columns by lifecycle state.

Each view renders from an immutable Snapshot and remembers its own
selection across refreshes, so a background refresh never moves the
cursor out from under you (spec §4.2).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist
from textual.worker import get_current_worker

from .format import group_header, jira_header, kanban_card, loading_row, provider_icon, row_widths, session_row
from .sessions import DONE_STATES, SessionState, Snapshot, SessionView


def attention_sort_key(view) -> tuple:
    """Order within/across ranks: most urgent rank, then most recently
    changed state, then most recent response.

    rank_stable_since (when the session last actually changed state) is the
    primary within-rank key so genuinely-WORKING sessions don't reshuffle on
    every token. But it lives only in memory: after an app restart every
    pre-existing session shares one timestamp, and without a further tiebreak
    the review queue silently degrades to project order. last_activity breaks
    that tie — frozen for finished sessions, so "needs review" reads as a real
    queue: newest response on top.

    review_fifo (review_oldest_first setting, default on): NEEDS_REVIEW rows
    are instead a FIFO line — the longest-waiting response first, keyed on
    the LATER of the last response and review_bumped_at (Ctrl+G sending a
    looked-at conversation to the back). last_activity rather than
    rank_stable_since so the order survives an app restart. Every row of the
    rank shares this key shape (rank is unique per state outside
    time_ordered_queue, which switches review_fifo off), so it never mixes
    with the newest-first tuple above."""
    state = getattr(view, "state", None)
    if state == SessionState.NEEDS_REVIEW and getattr(view, "review_fifo", False):
        finished = view.last_activity.timestamp() if view.last_activity else 0.0
        return (view.attention_rank, max(finished, getattr(view, "review_bumped_at", 0.0)), 0.0)
    last = view.last_activity.timestamp() if view.last_activity else 0.0
    if state in DONE_STATES:
        # Done and auto sleep are one pile, newest conversation first —
        # not by when each was finished.
        return (view.attention_rank, -last, 0.0)
    return (view.attention_rank, -view.rank_stable_since, -last)


class SessionList(OptionList):
    """OptionList with vim keys and stable-selection rebuilds."""

    # session_row builds its rows as Rich Text with no_wrap/overflow set, but
    # Textual converts a Text prompt to Content (visual.py: from_rich_text)
    # and takes wrapping from CSS instead — the Text's own flags are dropped.
    # Without these rules every row wider than the list folded in half, so a
    # one-line row became two: title on one, "finished, unreviewed" below it.
    DEFAULT_CSS = """
    SessionList {
        text-wrap: nowrap;
        text-overflow: ellipsis;
        scrollbar-gutter: stable;
    }
    /* Hover is not input: it wakes nothing, resets no clock, and shows nothing. */
    SessionList > .option-list--option-hover {
        background: transparent;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G", "last", "Last", show=False),
    ]

    @property
    def prompt_width(self) -> int | None:
        width = self.scrollable_content_region.width
        padding = self.get_component_styles("option-list--option").padding.width
        return max(1, width - padding) if width else None

    def on_resize(self) -> None:
        # Refit on every compact width change, not only when the app crosses
        # its compact breakpoint. The list's own layout includes its border,
        # option padding, and scrollbar, unlike the enclosing terminal width.
        if getattr(self.app, "compact", False) and isinstance(self.parent, (GroupedView, QueueView)):
            self.parent.update_snapshot(self.parent.snapshot)

    def _interacted(self) -> None:
        sid = self.highlighted_session_id
        if sid:
            self.post_message(SessionInteracted(sid))

    def action_cursor_up(self) -> None:
        if self.highlighted is None or any(not option.disabled for option in self.options[:self.highlighted]):
            super().action_cursor_up()

    def action_cursor_down(self) -> None:
        if self.highlighted is None or any(not option.disabled for option in self.options[self.highlighted + 1:]):
            super().action_cursor_down()

    _NAV_KEYS = ("j", "k", "g", "G", "up", "down", "home", "end", "pageup", "pagedown", "left", "right")

    def on_key(self, event) -> None:
        # Navigation is applied by OptionList's key handler. Rebuilds never
        # pass through here, so a timer cannot masquerade as human input.
        if event.key not in self._NAV_KEYS:
            return
        if self._key_moves_highlight(event.key):
            # The highlight moves AFTER this handler (and after the next
            # refresh), so reading it now would report the row being LEFT.
            # The visit is posted from on_option_list_option_highlighted.
            self._nav_pending = True
        else:
            self.call_after_refresh(self._interacted)  # re-visit of the same row

    def _key_moves_highlight(self, key: str) -> bool:
        index, count = self.highlighted, self.option_count
        if index is None or count == 0:
            return False
        if key in ("j", "down", "pagedown"):
            return index < count - 1
        if key in ("k", "up", "pageup"):
            return index > 0
        if key in ("g", "home"):
            return index != 0
        if key in ("G", "end"):
            return index != count - 1
        return False  # ← / →: kanban columns or the pane size, never this list's row

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if getattr(self, "_nav_pending", False):
            self._nav_pending = False
            if event.option.id is not None:
                self.post_message(SessionInteracted(event.option.id))

    def on_click(self, event) -> None:
        self.call_after_refresh(self._interacted)

    def rebuild(self, options: list[Option], keep_id: str | None) -> None:
        """Patch stable rows in place; rebuild only when membership/order changes.

        Clearing OptionList resets its scroll and emits transient highlights.
        Avoid both during refreshes and width changes.
        """
        self._nav_pending = False  # a key that ended up not moving the row
        old_index = self.highlighted
        old_scroll = self.scroll_offset
        same_rows = len(options) == self.option_count and all(
            (old.id, old.disabled) == (new.id, new.disabled)
            for old, new in zip(self.options, options)
        )
        with self.prevent(OptionList.OptionHighlighted):
            if same_rows:
                for i, (old, new) in enumerate(zip(self.options, options)):
                    if old.prompt != new.prompt:
                        self.replace_option_prompt_at_index(i, new.prompt)
            else:
                self.clear_options()
                self.add_options(options)
        if not options:
            return
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
        with self.prevent(OptionList.OptionHighlighted):
            self.highlighted = index
        if same_rows and index == old_index:
            return
        # Preserve the viewport across an atomic rebuild, moving only enough
        # to keep the selected conversation visible if its position changed.
        self.scroll_to(old_scroll.x, old_scroll.y, animate=False, force=True)
        self.scroll_to_highlight()

    @property
    def highlighted_session_id(self) -> str | None:
        if self.highlighted is None:
            return None
        try:
            option = self.get_option_at_index(self.highlighted)
        except Exception:
            return None
        return option.id


class SessionInteracted(Message):
    """Explicit mouse/keyboard input; distinct from a periodic re-highlight."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__()


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
        # In-flight placeholders (option_id, label) shown at the top of the
        # list while something slow (a handoff spec) is being produced.
        self.pending_rows: list[tuple[str, str]] = []
        # One-shot: the next update_snapshot should keep the cursor at its
        # current ROW INDEX rather than chasing selected_id to its new spot.
        # Set right before an explicit action (mark done, snooze, ...) that
        # moves the selected row elsewhere in this same list — otherwise the
        # cursor silently follows that session down instead of landing on
        # whatever now sits in the old spot.
        self._pin_cursor_next = False

    def pin_cursor_position(self) -> None:
        self._pin_cursor_next = True

    def _pending_options(self) -> list[Option]:
        return [
            Option(Text(f"  ⋯ {label}", style="italic dim"), id=option_id, disabled=True)
            for option_id, label in self.pending_rows
        ]

    def _loading_options(self, snapshot: Snapshot) -> list[Option]:
        if not snapshot.partial:
            return []
        return [Option(loading_row(snapshot.remaining), id=LOADING_ROW_ID, disabled=True)]

    def spin_loading_row(self) -> None:
        if not self.snapshot.partial:
            return
        for session_list in self.query(SessionList):
            try:
                session_list.replace_option_prompt(LOADING_ROW_ID, loading_row(self.snapshot.remaining))
            except OptionDoesNotExist:
                pass

    def _emit_selection(self, session_id: str | None) -> None:
        if session_id == self.selected_id:
            return
        self.selected_id = session_id
        self.post_message(SelectionChanged(self.id, session_id))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.disabled or event.option.id is None:
            return
        if event.option.id != event.option_list.highlighted_session_id:
            return  # an event queued before a rebuild or later keypress
        self._emit_selection(event.option.id)

    # Subclasses implement:
    def update_snapshot(self, snapshot: Snapshot) -> None:
        raise NotImplementedError

    def focus_target(self) -> "SessionList | None":
        """The list that focus_list() would focus."""
        raise NotImplementedError

    def focus_list(self) -> None:
        target = self.focus_target()
        if target is not None:
            target.focus()


LOADING_ROW_ID = "loading"


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
            groups.setdefault(view.group_dir, []).append(view)
        compact = bool(getattr(self.app, "compact", False))
        show_jira = bool(self.app.store.get_setting("jira_integration")) and not compact
        # One set of column widths for the whole list, so rows line up across
        # groups too.
        session_list = self.query_one("#grouped-list", SessionList)
        widths = row_widths(snapshot.views, self.app.store.get_setting("conversation_title_width"),
                            compact_width=session_list.prompt_width if compact else None)
        header = self.query_one("#grouped-jira-header", Static)
        header.set_class(show_jira, "shown")
        if show_jira:
            header.update(jira_header(widths))
        for project_dir in sorted(groups):
            views = groups[project_dir]
            options.append(
                Option(group_header(project_dir, len(views), compact=compact), disabled=True)
            )
            # Inside a group: most urgent first, newest state change /
            # newest response first — see attention_sort_key.
            views.sort(key=attention_sort_key)
            for view in views:
                options.append(
                    Option(
                        session_row(view, now, compact=compact, show_jira=show_jira, widths=widths),
                        id=view.session_id,
                    )
                )
        options = self._pending_options() + options + self._loading_options(snapshot)
        if not options:
            options = [_empty_option()]
        keep_id = None if self._pin_cursor_next else self.selected_id
        self._pin_cursor_next = False
        session_list.rebuild(options, keep_id)
        self._emit_selection(session_list.highlighted_session_id)

    def focus_target(self) -> SessionList:
        return self.query_one("#grouped-list", SessionList)


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
        # A real queue: urgency rank, then newest first — see attention_sort_key.
        ordered = sorted(snapshot.views, key=attention_sort_key)
        compact = bool(getattr(self.app, "compact", False))
        show_jira = bool(self.app.store.get_setting("jira_integration")) and not compact
        session_list = self.query_one("#queue-list", SessionList)
        widths = row_widths(ordered, self.app.store.get_setting("conversation_title_width"),
                            compact_width=session_list.prompt_width if compact else None)
        header = self.query_one("#queue-jira-header", Static)
        header.set_class(show_jira, "shown")
        if show_jira:
            header.update(jira_header(widths))
        options = [
            Option(
                session_row(
                    view, now, show_project=not compact, compact=compact, show_jira=show_jira, widths=widths
                ),
                id=view.session_id,
            )
            for view in ordered
        ]
        options = self._pending_options() + options + self._loading_options(snapshot)
        if not options:
            options = [_empty_option()]
        keep_id = None if self._pin_cursor_next else self.selected_id
        self._pin_cursor_next = False
        session_list.rebuild(options, keep_id)
        self._emit_selection(session_list.highlighted_session_id)

    def focus_target(self) -> SessionList:
        return self.query_one("#queue-list", SessionList)


# Kanban columns: (title, states shown, css id)
KANBAN_COLUMNS: list[tuple[str, tuple[SessionState, ...], str]] = [
    ("◉ Needs you", (SessionState.NEEDS_INPUT,), "kb-needs-you"),
    (
        "● Working",
        (SessionState.WORKING, SessionState.SHELL_RUNNING, SessionState.MONITORING, SessionState.BACKGROUND),
        "kb-working",
    ),
    ("☾ Snoozed", (SessionState.SNOOZED,), "kb-snoozed"),
    (
        "◆ To review",
        (SessionState.NEEDS_REVIEW, SessionState.EXTERNAL_UPDATE, SessionState.WAITING_EXTERNAL),
        "kb-review",
    ),
    ("✓ Done / stopped", (*DONE_STATES, SessionState.STOPPED), "kb-done"),
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
    /* Cards are deliberately multi-line (kanban_card) in a narrow column, so
       they keep folding — ellipsising them would cut every title short. */
    KanbanColumn > SessionList { text-wrap: wrap; }
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
    """Columns by lifecycle state; ← / → (or ⌃h/⌃l) move between columns."""

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
            views.sort(key=attention_sort_key)  # rank uniform per column; newest first
            options = [Option(kanban_card(view, now), id=view.session_id) for view in views]
            if column.list_id == "kb-working":
                options = self._pending_options() + options
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

    def focus_target(self) -> SessionList | None:
        columns = self._columns()
        if not columns:
            return None
        column = columns[min(self.active_column, len(columns) - 1)]
        return column.query_one(SessionList)

    def focus_list(self) -> None:
        super().focus_list()
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


_SEARCH_LOCK = threading.Lock()


def title_hits(views: list[SessionView], query: str) -> list[SessionView]:
    """Conversations whose title matches, best match first, newest first among equals."""
    from .search import fuzzy_score

    scored = [(score, view) for view in views if (score := fuzzy_score(query, view.title)) is not None]
    scored.sort(key=lambda hit: (-hit[0], -(hit[1].last_activity.timestamp() if hit[1].last_activity else 0.0)))
    return [view for _, view in scored]


class SearchView(Widget):
    """`/`: a search bar over the rail. Titles match in memory on every
    keystroke, best match first; ctrl+/ switches to full-history search.
    Enter takes the highlighted hit (the first, unless ↑/↓ moved it)."""

    DEFAULT_CSS = """
    SearchView { height: 1fr; }
    SearchView > #search-status { height: 1; padding: 0 1; color: $text-muted; }
    SearchView > OptionList { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "close", show=False),
        Binding("down,ctrl+n", "move(1)", show=False, priority=True),
        Binding("up,ctrl+p", "move(-1)", show=False, priority=True),
        Binding("ctrl+slash,ctrl+underscore", "toggle_full_history", show=False, priority=True),
    ]

    class Chosen(Message):
        def __init__(self, session_id: str, result=None) -> None:
            self.session_id = session_id
            self.result = result
            super().__init__()

    class Closed(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.views: list[SessionView] = []
        self.full_history = False
        self.results: list = []
        self._results_query = ""
        self._open_when_ready = False
        self._debounce = None

    def compose(self):
        yield Input(placeholder="search titles…", id="search-input")
        yield Static("", id="search-status")
        yield OptionList(id="search-results")

    @property
    def query_text(self) -> str:
        return self.query_one("#search-input", Input).value.strip()

    def open(self, snapshot: Snapshot) -> None:
        self.views = snapshot.views
        self.full_history = False
        self._open_when_ready = False
        search_input = self.query_one("#search-input", Input)
        search_input.value = ""
        self._show_titles()
        search_input.focus()

    def update_snapshot(self, snapshot: Snapshot) -> None:
        self.views = snapshot.views
        if not self.full_history:
            self._show_titles()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._open_when_ready = False
        if not self.full_history:
            self._show_titles()
            return
        if self._debounce is not None:
            self._debounce.stop()
        query = self.query_text
        if not query:
            self._show_results("", [])
            return
        self._debounce = self.set_timer(0.06, lambda: self._run_full_history_search(query))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.full_history and self._results_query != self.query_text:
            self._open_when_ready = True
            return
        self._choose(self.query_one("#search-results", OptionList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(event.option_index)

    def _choose(self, index: int | None) -> None:
        results = self.query_one("#search-results", OptionList)
        if index is None or index >= results.option_count:
            return
        session_id = results.get_option_at_index(index).id
        result = next((r for r in self.results if r.session_id == session_id), None) if self.full_history else None
        self.post_message(self.Chosen(session_id, result))

    def action_close(self) -> None:
        self.post_message(self.Closed())

    def action_move(self, step: int) -> None:
        results = self.query_one("#search-results", OptionList)
        if results.option_count:
            current = results.highlighted or 0
            results.highlighted = max(0, min(results.option_count - 1, current + step))

    def action_toggle_full_history(self) -> None:
        if not self.full_history and not self.app.store.get_setting("conversation_search"):
            self.app.notify("Conversation search is off — enable it in Settings (,).", severity="warning")
            return
        self.full_history = not self.full_history
        self.query_one("#search-input", Input).placeholder = (
            "search full history…" if self.full_history else "search titles…")
        if self.full_history:
            self._show_results("", [])
            self._run_full_history_search(self.query_text)
        else:
            self._show_titles()

    def _show_titles(self) -> None:
        query = self.query_text
        hits = title_hits(self.views, query) if query else []
        results = self.query_one("#search-results", OptionList)
        widths = row_widths(hits, self.app.store.get_setting("conversation_title_width"))
        now = datetime.now(timezone.utc)
        results.set_options([Option(session_row(view, now, show_project=True, widths=widths), id=view.session_id)
                             for view in hits])
        if hits:
            results.highlighted = 0
        noun = "title match" if len(hits) == 1 else "title matches"
        self.query_one("#search-status", Static).update(
            f"{len(hits)} {noun} · ctrl+/ searches full history" if query
            else "type to search titles · ctrl+/ searches full history")

    @work(thread=True, exclusive=True, group="conversation-search")
    def _run_full_history_search(self, query: str) -> None:
        from .search import scan_all_sessions, search_all_sessions

        with _SEARCH_LOCK:
            if get_current_worker().is_cancelled:
                return
            if not query:
                scan_all_sessions(self.app.claude_dir, codex_dir=self.app.codex_dir)
                return
            results = search_all_sessions(self.app.claude_dir, query, codex_dir=self.app.codex_dir)
        self.app.call_from_thread(self._show_results, query, results)

    def _show_results(self, query: str, results: list) -> None:
        from .search import MatchKind

        if not self.full_history or query != self.query_text:
            return
        self.results, self._results_query = results, query
        if self._open_when_ready and results:
            self._open_when_ready = False
            self.post_message(self.Chosen(results[0].session_id, results[0]))
            return
        options = []
        for result in results:
            is_title_match = result.kind in (MatchKind.TITLE_EXACT, MatchKind.TITLE_FUZZY)
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append_text(provider_icon("codex" if result.session_id.startswith("codex:") else "claude"))
            row.append(" ")
            row.append(f"{result.title[:60]:<60}", style="bold cyan" if is_title_match else "bold")
            row.append(f"  [{result.kind.label}]\n", style="dim italic")
            row.append(f"  {result.project_dir}\n", style="dim cyan")
            row.append(f"  {result.snippet}", style="italic dim")
            options.append(Option(row, id=result.session_id))
        results_list = self.query_one("#search-results", OptionList)
        results_list.set_options(options)
        if options:
            results_list.highlighted = 0
        noun = "match" if len(results) == 1 else "matches"
        self.query_one("#search-status", Static).update(
            f"full history · {len(results)} {noun} · ctrl+/ titles" if query
            else "type to search full history · ctrl+/ titles")
