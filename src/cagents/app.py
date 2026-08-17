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
from . import gitops
from .diffview import DiffResult, DiffScreen
from .modals import (
    ConfirmModal,
    HelpModal,
    InputModal,
    NewSessionModal,
    PaletteModal,
    PlanConfirmModal,
    TodoModal,
    TrackModal,
)
from .palette import CliClaudeRunner, apply_plan, build_prompt, parse_plan
from .peek import PeekScreen, deep_view
from .sessions import SessionRegistry, SessionState, SessionView, Snapshot
from .sidecar import Sidecar, nested_attach_command
from .store import Store
from .tmuxctl import TmuxClient
from .views import GroupedView, KanbanView, QueueView, SelectionChanged, TodoSelected, TodoView

REFRESH_SECONDS = 2.0
COMPACT_WIDTH = 60  # below this, the UI is a rail: no preview, dense rows

VIEW_IDS = ["grouped", "queue", "kanban", "todos"]


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
    #body.compact #preview-pane { display: none; }
    """

    BINDINGS = [
        Binding("1", "switch_view('grouped')", "Grouped"),
        Binding("2", "switch_view('queue')", "Queue"),
        Binding("3", "switch_view('kanban')", "Kanban"),
        Binding("4", "switch_view('todos')", "Todos"),
        Binding("D", "show_diff", "Diff"),
        Binding("tab", "next_view", "Next view", show=False, priority=True),
        Binding("enter", "attach", "Attach", priority=False, show=True),
        Binding("space", "peek", "Peek"),
        Binding("o", "open_link", "Open link", show=False),
        Binding("colon", "palette", "Fleet ':'", show=False),
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
        claude_runner=None,
        sidecar: Sidecar | None = None,
    ):
        super().__init__()
        self.claude_runner = claude_runner  # lazy CliClaudeRunner if None
        self.sidecar = sidecar if sidecar is not None else (Sidecar() if Sidecar.enabled() else None)
        self.compact = False
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
                yield TodoView(id="todos")
            with VerticalScroll(id="preview-pane"):
                yield Static(id="preview-content")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_compact(self.size.width)
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)
        self.query_one("#grouped", GroupedView).focus_list()

    def on_resize(self, event) -> None:
        self._apply_compact(event.size.width)

    def _apply_compact(self, width: int) -> None:
        """Below ~60 columns (the collapsed sidecar rail) drop the preview
        pane and switch rows to their dense form. States keep ticking."""
        compact = width < COMPACT_WIDTH
        if compact == self.compact:
            return
        self.compact = compact
        try:
            self.query_one("#body").set_class(compact, "compact")
        except Exception:
            return  # before compose finishes
        for view_id in VIEW_IDS:
            self.query_one(f"#{view_id}").update_snapshot(self.snapshot)

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

    def on_todo_selected(self, event: TodoSelected) -> None:
        # In the todo view, session-level actions (attach/peek/diff/review)
        # target the todo's newest session.
        if self.active_view_id == "todos":
            self.selected_session_id = event.session_id
            self._update_preview()

    def selected_todo(self):
        todo_view = self.query_one("#todos", TodoView)
        if todo_view.selected_todo_id is None:
            return None
        return self.store.todos.get(todo_view.selected_todo_id)

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
        if event.option.id is None:
            return
        if event.option.id.startswith("todo:"):
            # Enter on a todo: attach to its newest session (if any).
            if self.selected_session_id:
                self.action_attach()
            else:
                self.notify("No sessions for this todo yet — press 'n' to start one.")
            return
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
                self._attach_tmux_session(view.tmux_name)
            else:
                self._resume_dead_session(view)
        except Exception as error:  # loud, specific failure (spec §11)
            self.notify(f"Attach failed: {error}", severity="error", timeout=10)
        self.refresh_data()

    def _attach_tmux_session(self, name: str) -> None:
        """Hand off to the real CLI: full-terminal suspend normally, or the
        right-hand pane when running as a sidecar rail."""
        if self.sidecar is not None:
            self.sidecar.open(nested_attach_command(self.tmux.socket, name))
        else:
            self._suspend_and_run(lambda: self.tmux.attach(name))

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
        self._attach_tmux_session(name)

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
        self._pending_todo_link = None
        if self.active_view_id == "todos":
            todo = self.selected_todo()
            if todo is not None:
                self._pending_todo_link = todo.todo_id
                initial = todo.worktree or todo.project_dir or os.getcwd()
                self.push_screen(NewSessionModal(initial), self._new_session_chosen)
                return
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
        pending = getattr(self, "_pending_todo_link", None)
        if pending:
            self.store.link_todo_session(pending, session_id)
            self._pending_todo_link = None
        self.selected_session_id = session_id
        self._attach_tmux_session(name)
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

    # -- peek / links ------------------------------------------------------------

    def action_peek(self) -> None:
        view = self.selected_view()
        if view is None:
            self.notify("No session selected.", severity="warning")
            return
        deep = deep_view(view)
        self.push_screen(PeekScreen(deep), lambda r: self._peek_closed(view.session_id, r))

    def _peek_closed(self, session_id: str, result: str | None) -> None:
        if result == "reviewed":
            self.store.mark_reviewed(session_id, utcnow().isoformat())
            self.notify("Marked reviewed.")
            self.refresh_data()

    def action_open_link(self) -> None:
        view = self.selected_view()
        if view is None or not view.parsed or not view.parsed.links:
            self.notify("No links recorded for this session.", severity="warning")
            return
        link = view.parsed.links[-1]
        import subprocess

        try:
            subprocess.run(["open", link.url], check=True, timeout=10)
            self.notify(f"Opened {link.label}.")
        except Exception as error:
            self.notify(f"Could not open {link.label}: {error}", severity="error")

    # -- fleet palette (the explicitly-labeled AI surface) ------------------------

    def action_palette(self) -> None:
        self.push_screen(PaletteModal(), self._palette_submitted)

    def _palette_submitted(self, request: str | None) -> None:
        if not request:
            return
        self.notify("Asking the fleet assistant… (plan will need your confirmation)")
        self._run_palette_request(request)

    @work(thread=True, exclusive=True, group="palette")
    def _run_palette_request(self, request: str) -> None:
        snapshot = self.snapshot
        runner = self.claude_runner or CliClaudeRunner(claude_bin=self._claude_bin())
        try:
            raw = runner.run(build_prompt(snapshot, request))
            plan = parse_plan(raw, snapshot)
        except Exception as error:  # loud, specific (spec §11)
            self.call_from_thread(
                self.notify, f"Fleet assistant failed: {error}", severity="error", timeout=10
            )
            return
        titles = {v.session_id: v.title for v in snapshot.views}
        self.call_from_thread(
            self.push_screen, PlanConfirmModal(plan, titles), lambda yes: self._plan_confirmed(plan, yes)
        )

    def _plan_confirmed(self, plan, yes: bool) -> None:
        if not yes:
            return
        done = apply_plan(plan, self.store, utcnow().isoformat())
        self.notify("Applied: " + ", ".join(done) if done else "Nothing to apply.")
        self.refresh_data()

    # -- todos ---------------------------------------------------------------------

    def action_add_todo(self) -> None:
        view = self.selected_view()
        initial = view.project_dir if view else ""
        self.push_screen(TodoModal(initial), self._todo_added)

    def _todo_added(self, result: tuple[str, str] | None) -> None:
        if not result:
            return
        text, directory = result
        self.store.add_todo(text, utcnow().isoformat(), project_dir=directory)
        self.refresh_data()
        self.notify("Todo added.")

    def action_toggle_todo_done(self) -> None:
        todo = self.selected_todo()
        if todo is None:
            return
        if todo.done:
            self.store.set_todo_done(todo.todo_id, "")
            for sid in todo.session_ids:
                self.store.set_archived(sid, False)
            self.notify("Todo reopened; its workspaces are back in the views.")
            self.refresh_data()
            return
        self.store.set_todo_done(todo.todo_id, utcnow().isoformat())
        linked = [sid for sid in todo.session_ids if sid in self.store.sessions]
        if not linked and not todo.worktree:
            self.notify("Todo done.")
            self.refresh_data()
            return
        what = []
        if linked:
            what.append(f"{len(linked)} session{'s' if len(linked) != 1 else ''}")
        if todo.worktree:
            what.append(f"worktree {todo.worktree}")
        self.push_screen(
            ConfirmModal(
                f"Todo done. Archive its workspace ({', '.join(what)})?\n\n"
                "Sessions are hidden from views (history kept); a clean worktree "
                "is removed — a dirty one refuses loudly."
            ),
            lambda yes: self._archive_todo_workspace(todo.todo_id, bool(yes)),
        )

    def _archive_todo_workspace(self, todo_id: str, yes: bool) -> None:
        if not yes:
            self.refresh_data()
            return
        todo = self.store.todos.get(todo_id)
        if todo is None:
            return
        for sid in todo.session_ids:
            self.store.set_archived(sid, True)
        if todo.worktree:
            self._remove_worktree_worker(todo_id)
        else:
            self.notify("Workspace archived.")
        self.refresh_data()

    @work(thread=True, exclusive=True, group="worktree")
    def _remove_worktree_worker(self, todo_id: str) -> None:
        todo = self.store.todos.get(todo_id)
        if todo is None or not todo.worktree:
            return
        try:
            gitops.remove_worktree(todo.project_dir or todo.worktree, todo.worktree)
        except gitops.GitError as error:
            self.call_from_thread(
                self.notify,
                f"Worktree kept: {error}",
                severity="warning",
                timeout=10,
            )
            return
        self.call_from_thread(self._worktree_removed, todo_id)

    def _worktree_removed(self, todo_id: str) -> None:
        self.store.set_todo_worktree(todo_id, "")
        self.notify("Workspace archived; worktree removed.")
        self.refresh_data()

    def action_delete_todo(self) -> None:
        todo = self.selected_todo()
        if todo is None:
            return
        self.push_screen(
            ConfirmModal(
                f"Delete todo '{todo.text}'?\n\nIts sessions and worktree are untouched."
            ),
            lambda yes: self._todo_deleted(todo.todo_id, bool(yes)),
        )

    def _todo_deleted(self, todo_id: str, yes: bool) -> None:
        if not yes:
            return
        self.store.delete_todo(todo_id)
        self.refresh_data()

    def action_todo_worktree(self) -> None:
        todo = self.selected_todo()
        if todo is None:
            return
        if todo.worktree:
            self.notify(f"This todo already has a worktree: {todo.worktree}")
            return
        repo = todo.project_dir
        if not repo or not gitops.is_git_repo(repo):
            self.notify(
                "Todo needs a project directory that is a git repo (edit the todo's project).",
                severity="warning",
                timeout=8,
            )
            return
        self.notify("Creating worktree…")
        self._create_worktree_worker(todo.todo_id, repo, todo.text)

    @work(thread=True, exclusive=True, group="worktree")
    def _create_worktree_worker(self, todo_id: str, repo: str, name: str) -> None:
        try:
            path = gitops.create_worktree(repo, name)
        except gitops.GitError as error:
            self.call_from_thread(
                self.notify, f"Worktree failed: {error}", severity="error", timeout=10
            )
            return
        self.call_from_thread(self._worktree_created, todo_id, path)

    def _worktree_created(self, todo_id: str, path: str) -> None:
        self.store.set_todo_worktree(todo_id, path)
        claude_bin = self._claude_bin()
        if not claude_bin:
            self.notify(f"Worktree ready at {path} (claude CLI not found to start a session).")
            return
        session_id = str(uuid.uuid4())
        try:
            name = self.tmux.new_claude_session(
                path, ["--session-id", session_id], session_id=session_id, claude_bin=claude_bin
            )
        except Exception as error:
            self.notify(f"Worktree ready at {path}, but session failed: {error}", severity="error")
            return
        self.store.track(session_id, path, utcnow().isoformat())
        self.store.link_todo_session(todo_id, session_id)
        self.selected_session_id = session_id
        self._attach_tmux_session(name)
        self.refresh_data()

    # -- diff review ------------------------------------------------------------------

    def action_show_diff(self) -> None:
        view = self.selected_view()
        directory = ""
        if self.active_view_id == "todos":
            todo = self.selected_todo()
            if todo is not None:
                directory = todo.worktree or todo.project_dir
        if not directory and view is not None:
            directory = view.project_dir
        if not directory:
            self.notify("Nothing selected to diff.", severity="warning")
            return
        self.notify("Building diff…")
        self._diff_worker(directory)

    @work(thread=True, exclusive=True, group="diff")
    def _diff_worker(self, directory: str) -> None:
        try:
            diff = gitops.worktree_diff(directory)
        except gitops.GitError as error:
            self.call_from_thread(self.notify, f"Diff failed: {error}", severity="error", timeout=10)
            return
        self.call_from_thread(self._show_diff_screen, diff)

    def _show_diff_screen(self, diff) -> None:
        view = self.selected_view()
        target = f"'{view.title}'" if view else "the session"
        screen = DiffScreen(
            diff,
            target_desc=target,
            github_puller=lambda d=diff.directory: gitops.github_pr_comments(d),
        )
        session_id = view.session_id if view else None
        self.push_screen(screen, lambda result: self._diff_closed(session_id, result))

    def _diff_closed(self, session_id: str | None, result: DiffResult | None) -> None:
        if result is None or not result.send_message:
            return
        if session_id is None:
            self.notify("No session to send the comments to.", severity="warning")
            return
        view = self.snapshot.by_id(session_id)
        if view is None:
            self.notify("Session vanished from the snapshot.", severity="error")
            return
        self._send_review_worker(view, result.send_message, result.comment_count)

    @work(thread=True, exclusive=True, group="send")
    def _send_review_worker(self, view: SessionView, message: str, count: int) -> None:
        import time

        try:
            tmux_name = view.tmux_name
            if not view.live:
                # Resume the session first, give the CLI a moment to boot.
                tmux_name = self.tmux.new_claude_session(
                    view.project_dir,
                    ["--resume", view.session_id],
                    session_id=view.session_id,
                    claude_bin=self._claude_bin(),
                )
                time.sleep(4.0)
            self.tmux.send_text(tmux_name, message)
        except Exception as error:
            self.call_from_thread(
                self.notify,
                f"Could not deliver comments: {error}",
                severity="error",
                timeout=10,
            )
            return
        self.call_from_thread(
            self.notify,
            f"Sent {count} review comment{'s' if count != 1 else ''} to Claude "
            f"(attach with Enter to watch).",
        )
        self.call_from_thread(self.refresh_data)

    # -- misc -------------------------------------------------------------------

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpModal())
