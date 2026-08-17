"""The cagents application shell.

Responsibilities:
- keep one fresh Snapshot flowing into whichever view is active
  (refresh runs in a worker thread; the UI never blocks on I/O);
- hand off to the real Claude CLI on Enter (spec §4.4) via tmux on the
  user's existing `claude` socket;
- own the tiny bits of human review state (reviewed / note / label).
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import ContentSwitcher, Footer, OptionList, Static

from .claude_data import default_claude_dir, parse_session_file, utcnow
from .format import header_summary, preview_renderable
from .modals import ConfirmModal, HelpModal, InputModal, NewSessionModal, TrackModal
from .sessions import SessionRegistry, SessionState, SessionView, Snapshot
from .store import Store
from .tmuxctl import TmuxClient
from .views import GroupedView, KanbanView, QueueView, SelectionChanged

REFRESH_SECONDS = 2.0

VIEW_IDS = ["grouped", "queue", "kanban"]


class CagentsApp(App):
    TITLE = "cagents"

    CSS = """
    #summary {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #body { height: 1fr; }
    #views { width: 1fr; height: 1fr; }
    #preview-pane {
        width: 44%;
        min-width: 30;
        height: 1fr;
        border-left: solid $surface-lighten-2;
        padding: 0 1;
    }
    #body.kanban #preview-pane { display: none; }
    """

    BINDINGS = [
        Binding("1", "switch_view('grouped')", "Grouped"),
        Binding("2", "switch_view('queue')", "Queue"),
        Binding("3", "switch_view('kanban')", "Kanban"),
        Binding("tab", "next_view", "Next view", show=False, priority=True),
        Binding("enter", "attach", "Attach", priority=False, show=True),
        Binding("n", "new_session", "New"),
        Binding("a", "track_session", "Track"),
        Binding("r", "toggle_reviewed", "Reviewed"),
        Binding("e", "edit_note", "Note", show=False),
        Binding("L", "edit_label", "Label", show=False),
        Binding("x", "untrack", "Untrack", show=False),
        Binding("R", "refresh_now", "Refresh", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        store: Store | None = None,
        registry: SessionRegistry | None = None,
        tmux: TmuxClient | None = None,
        claude_dir: Path | None = None,
    ):
        super().__init__()
        self.store = store or Store.load()
        self.tmux = tmux or TmuxClient()
        self.claude_dir = claude_dir or default_claude_dir()
        self.registry = registry or SessionRegistry(
            self.store, tmux=self.tmux, claude_dir=self.claude_dir
        )
        self.snapshot = Snapshot()
        self.active_view_id = "grouped"
        self.selected_session_id: str | None = None

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        with Horizontal(id="body"):
            with ContentSwitcher(initial="grouped", id="views"):
                yield GroupedView(id="grouped")
                yield QueueView(id="queue")
                yield KanbanView(id="kanban")
            with VerticalScroll(id="preview-pane"):
                yield Static(id="preview-content")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)
        self.query_one("#grouped", GroupedView).focus_list()

    def check_action(self, action: str, parameters) -> bool:
        # App-level keys must not fire behind a modal (e.g. 'q' while a
        # confirm dialog's list has focus).
        from textual.screen import ModalScreen

        if isinstance(self.screen, ModalScreen):
            # `next_view` must yield so the modal's own tab binding
            # (focus_next between fields) can take over.
            return action in ("focus_next", "focus_previous")
        return True

    # -- data flow -----------------------------------------------------------

    def refresh_data(self) -> None:
        self._refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh_worker(self) -> None:
        snapshot = self.registry.refresh()
        self.call_from_thread(self.apply_snapshot, snapshot)

    def apply_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.query_one("#summary", Static).update(header_summary(snapshot.counts()))
        for view_id in VIEW_IDS:
            self.query_one(f"#{view_id}").update_snapshot(snapshot)
        self._update_preview()

    def current_view(self):
        return self.query_one(f"#{self.active_view_id}")

    def selected_view(self) -> SessionView | None:
        if self.selected_session_id is None:
            return None
        return self.snapshot.by_id(self.selected_session_id)

    def on_selection_changed(self, event: SelectionChanged) -> None:
        # Only the active view drives the preview.
        if event.view_id == self.active_view_id:
            self.selected_session_id = event.session_id
            self._update_preview()

    def _update_preview(self) -> None:
        content = self.query_one("#preview-content", Static)
        view = self.selected_view()
        if view is None:
            content.update("")
            return
        pane = self.query_one("#preview-pane", VerticalScroll)
        width = max(40, pane.size.width - 2)
        content.update(preview_renderable(view, datetime.now(timezone.utc), width=width))
        pane.scroll_end(animate=False)

    # -- view switching --------------------------------------------------------

    def action_switch_view(self, view_id: str) -> None:
        self.active_view_id = view_id
        self.query_one("#views", ContentSwitcher).current = view_id
        self.query_one("#body").set_class(view_id == "kanban", "kanban")
        view = self.current_view()
        view.update_snapshot(self.snapshot)
        view.focus_list()

    def action_next_view(self) -> None:
        i = VIEW_IDS.index(self.active_view_id)
        self.action_switch_view(VIEW_IDS[(i + 1) % len(VIEW_IDS)])

    # -- attaching (the core of the core loop) ---------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.selected_session_id = event.option.id
            self.action_attach()

    def action_attach(self) -> None:
        view = self.selected_view()
        if view is None:
            self.notify("No session selected.", severity="warning")
            return
        if not self.tmux.available():
            self.notify("tmux not found on PATH — cannot attach.", severity="error")
            return
        try:
            if view.live:
                self._suspend_and_run(lambda: self.tmux.attach(view.tmux_name))
            else:
                self._resume_dead_session(view)
        except Exception as error:  # loud, specific failure (spec §11)
            self.notify(f"Attach failed: {error}", severity="error", timeout=10)
        self.refresh_data()

    def _resume_dead_session(self, view: SessionView) -> None:
        if view.missing:
            self.notify(
                "This session's transcript is gone from Claude's store; nothing to resume.",
                severity="error",
                timeout=10,
            )
            return
        directory = view.project_dir
        if not Path(directory).is_dir():
            self.notify(f"Project directory no longer exists: {directory}", severity="error", timeout=10)
            return
        claude_bin = self._claude_bin()
        if not claude_bin:
            self.notify("claude CLI not found.", severity="error")
            return
        name = self.tmux.new_claude_session(
            directory,
            ["--resume", view.session_id],
            session_id=view.session_id,
            claude_bin=claude_bin,
        )
        self._suspend_and_run(lambda: self.tmux.attach(name))

    def _suspend_and_run(self, fn) -> None:
        from textual.app import SuspendNotSupported

        try:
            with self.suspend():
                fn()
        except SuspendNotSupported:
            # Headless (tests) or an unusual driver: run without handing
            # over the terminal rather than failing the attach outright.
            fn()

    def _claude_bin(self) -> str:
        found = shutil.which("claude")
        if found:
            return found
        fallback = Path.home() / ".local" / "bin" / "claude"
        return str(fallback) if fallback.exists() else ""

    # -- creating / tracking sessions ------------------------------------------

    def action_new_session(self) -> None:
        view = self.selected_view()
        initial = view.project_dir if view else os.getcwd()
        self.push_screen(NewSessionModal(initial), self._new_session_chosen)

    def _new_session_chosen(self, result: tuple[str, str] | None) -> None:
        if not result:
            return
        directory, label = result
        claude_bin = self._claude_bin()
        if not claude_bin:
            self.notify("claude CLI not found.", severity="error")
            return
        session_id = str(uuid.uuid4())
        try:
            name = self.tmux.new_claude_session(
                directory,
                ["--session-id", session_id],
                session_id=session_id,
                claude_bin=claude_bin,
            )
        except Exception as error:
            self.notify(f"Could not start session: {error}", severity="error", timeout=10)
            return
        self.store.track(session_id, directory, utcnow().isoformat(), label=label)
        self.selected_session_id = session_id
        self._suspend_and_run(lambda: self.tmux.attach(name))
        self.refresh_data()

    def action_track_session(self) -> None:
        self._load_track_candidates()

    @work(thread=True, exclusive=True, group="track")
    def _load_track_candidates(self) -> None:
        candidates = []
        for discovered in self.registry.discover_untracked()[:200]:
            title = discovered.session_id[:8]
            try:
                parsed = parse_session_file(
                    discovered.path, head_bytes=16 * 1024, tail_bytes=32 * 1024, preview_items=1
                )
                title = parsed.title
                cwd = parsed.cwd
            except OSError:
                cwd = ""
            candidates.append((discovered, title, cwd))
        self.call_from_thread(self._show_track_modal, candidates)

    def _show_track_modal(self, candidates: list) -> None:
        if not candidates:
            self.notify("No untracked sessions found in Claude's store.")
            return
        self._track_cwds = {d.session_id: cwd for d, _t, cwd in candidates}
        self.push_screen(
            TrackModal([(d, t) for d, t, _cwd in candidates]), self._track_chosen
        )

    def _track_chosen(self, session_id: str | None) -> None:
        if not session_id:
            return
        cwd = getattr(self, "_track_cwds", {}).get(session_id, "")
        self.store.track(session_id, cwd or str(Path.home()), utcnow().isoformat())
        self.selected_session_id = session_id
        self.refresh_data()
        self.notify("Session tracked.")

    # -- review state / note / label -------------------------------------------

    def action_toggle_reviewed(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        if view.state == SessionState.DONE:
            self.store.clear_reviewed(view.session_id)
            self.notify("Review cleared — back to 'needs review'.")
        elif view.state in (SessionState.NEEDS_REVIEW, SessionState.STOPPED):
            self.store.mark_reviewed(view.session_id, utcnow().isoformat())
            self.notify("Marked reviewed.")
        else:
            self.notify("Still in flight — review it when Claude is finished.", severity="warning")
            return
        self.refresh_data()

    def action_edit_note(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        self.push_screen(
            InputModal("Note", initial=view.tracked.note, placeholder="short note to self"),
            lambda note: self._note_saved(view.session_id, note),
        )

    def _note_saved(self, session_id: str, note: str | None) -> None:
        if note is None:
            return
        self.store.set_note(session_id, note.strip())
        self.refresh_data()

    def action_edit_label(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        self.push_screen(
            InputModal("Label", initial=view.tracked.label, placeholder="overrides the AI title"),
            lambda label: self._label_saved(view.session_id, label),
        )

    def _label_saved(self, session_id: str, label: str | None) -> None:
        if label is None:
            return
        self.store.set_label(session_id, label.strip())
        self.refresh_data()

    def action_untrack(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        self.push_screen(
            ConfirmModal(
                f"Untrack '{view.title}'?\n\nOnly removes it from cagents — "
                "Claude's own session data is untouched."
            ),
            lambda yes: self._untrack_confirmed(view.session_id, bool(yes)),
        )

    def _untrack_confirmed(self, session_id: str, yes: bool) -> None:
        if not yes:
            return
        self.store.untrack(session_id)
        if self.selected_session_id == session_id:
            self.selected_session_id = None
        self.refresh_data()

    # -- misc -------------------------------------------------------------------

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpModal())
