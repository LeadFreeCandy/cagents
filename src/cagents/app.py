"""The cagents application shell.

Responsibilities:
- keep one fresh Snapshot flowing into whichever view is active
  (refresh runs in a worker thread; the UI never blocks on I/O);
- hand off to the real Claude CLI on Enter (spec §4.4) via tmux on the
  user's existing `claude` socket;
- own the tiny bits of human review state (reviewed / note / label).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cagents.app")

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
    SettingsModal,
    TodoModal,
    TrackModal,
)
from .handoff import first_message, summary_prompt
from .modals import PauseModal, RelatedModal, ScriptConfirmModal
from .notifier import notify_desktop, read_select_request
from .plugins import PluginAPI, PluginManager, PLUGIN_GUIDE
from .palette import CliClaudeRunner, apply_plan, build_prompt, parse_plan
from .wake import WakeEngine, build_wake_prompt, extract_script, iso_in, parse_duration
from .sessions import SessionRegistry, SessionState, SessionView, Snapshot
from .sidecar import Sidecar, apply_left_capture, nested_attach_command
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
        Binding("F", "fork", "Fork", show=False),
        Binding("H", "handoff", "Handoff", show=False),
        Binding("asterisk", "related", "Related", show=False),
        Binding("plus", "add_plugin", "Plugin", show=False),
        Binding("m", "toggle_monitoring", "Monitor", show=False),
        Binding("t", "split_shell", "Shell", show=False),
        Binding("V", "rich_diff", "Rich diff", show=False),
        Binding("o", "open_link", "Open link", show=False),
        Binding("colon", "palette", "Fleet ':'", show=False),
        Binding("n", "new_session", "New"),
        Binding("a", "track_session", "Track"),
        Binding("r", "toggle_reviewed", "Reviewed"),
        Binding("e", "edit_note", "Note", show=False),
        Binding("L", "edit_label", "Label", show=False),
        Binding("x", "untrack", "Untrack", show=False),
        Binding("R", "refresh_now", "Refresh", show=False),
        Binding("equals_sign", "expand_rail", "Expand", show=False),
        Binding("comma", "settings", "Settings", show=False),
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
        self._preview_tmux_name: str | None = None  # what the sidecar's right pane is showing
        self._recently_started: dict[str, str] = {}  # session_id -> tmux name we just spawned
        logger.info(
            "CagentsApp constructed: sidecar=%s sidebar_setting=%s",
            self.sidecar is not None, self.store.get_setting("sidebar"),
        )
        self.wake_engine = WakeEngine(self.store)
        self._prev_states: dict[str, SessionState] = {}
        self._seen_first_snapshot = False
        self.plugins = PluginManager(self.store.path.parent / "plugins")

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
        self.set_interval(60.0, self._wake_tick)
        self.query_one("#grouped", GroupedView).focus_list()
        if os.environ.get("CAGENTS_SIDECAR") == "1" and self.sidecar is not None:
            try:
                apply_left_capture(self.store.get_setting("capture_left"))
            except Exception as error:
                self.notify(f"Left-arrow binding failed: {error}", severity="warning")

    def notify(self, message, *, title="", severity="information", timeout=None, **kwargs):
        """Routine toasts are opt-in (settings: notifications, default off).
        Warnings and errors always show — silent failure is the one
        unforgivable sin here (spec §11)."""
        if severity == "information" and not self.store.get_setting("notifications"):
            return None
        if timeout is None:
            return super().notify(message, title=title, severity=severity, **kwargs)
        return super().notify(message, title=title, severity=severity, timeout=timeout, **kwargs)

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
        self._notify_transitions(snapshot)
        self._handle_select_request()
        self._run_plugin_hooks(snapshot)

    def _run_plugin_hooks(self, snapshot: Snapshot) -> None:
        for error in self.plugins.scan():
            self.notify(f"Plugin error: {error}", severity="warning", timeout=10)
        api = PluginAPI(self)
        for record in self.plugins.snapshot_hooks():
            try:
                record.on_snapshot(api, snapshot)
            except Exception as error:
                record.error = f"on_snapshot: {error}"
                self.notify(f"Plugin '{record.name}' failed: {error}", severity="warning")

    def on_key(self, event) -> None:
        # Plugin keybinds: only keys no built-in binding claimed reach here.
        record = self.plugins.by_key(event.key)
        if record is None:
            return
        event.stop()
        api = PluginAPI(self)
        try:
            record.run(api)
        except Exception as error:
            record.error = f"run: {error}"
            self.notify(f"Plugin '{record.name}' failed: {error}", severity="error")

    def _notify_transitions(self, snapshot: Snapshot) -> None:
        """Desktop-notify sessions that just started needing a human.
        Edge-triggered; the first snapshot never notifies (startup is not
        news)."""
        alert_states = (SessionState.NEEDS_INPUT, SessionState.NEEDS_REVIEW)
        first = not self._seen_first_snapshot
        self._seen_first_snapshot = True
        enabled = bool(self.store.get_setting("desktop_notifications"))
        for view in snapshot.views:
            previous = self._prev_states.get(view.session_id)
            self._prev_states[view.session_id] = view.state
            if first or not enabled:
                continue
            if view.state in alert_states and previous not in alert_states and previous is not None:
                self.run_worker(
                    lambda v=view: notify_desktop(
                        f"cagents: {v.state.value}",
                        f"{v.title} — {v.needs_line or v.state_detail}",
                        v.session_id,
                        self.store.path.parent,
                    ),
                    thread=True,
                )

    def _handle_select_request(self) -> None:
        """A clicked desktop notification asked us to select a session."""
        session_id = read_select_request(self.store.path.parent)
        if not session_id or self.snapshot.by_id(session_id) is None:
            return
        if self.active_view_id not in ("grouped", "queue"):
            self.action_switch_view("queue")
        from .views import SessionList

        session_list = self.query_one(f"#{self.active_view_id}-list", SessionList)
        for i in range(session_list.option_count):
            option = session_list.get_option_at_index(i)
            if option.id == session_id:
                session_list.highlighted = i
                break

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
        # In the todo view, session-level actions (attach/diff/review)
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
            self._sync_sidecar_preview(None)
            return
        pane = self.query_one("#preview-pane", VerticalScroll)
        width = max(40, pane.size.width - 2)
        content.update(preview_renderable(view, datetime.now(timezone.utc), width=width))
        pane.scroll_end(animate=False)
        self._sync_sidecar_preview(view)

    def _sync_sidecar_preview(self, view: SessionView | None) -> None:
        """Mirror the highlighted session into the sidecar's right pane as a
        live, read-only tmux attach — the real Claude Code render, always
        current as selection moves, rather than a re-implemented summary.
        Only touches the pane when the target actually changes, so it never
        interrupts an interactive attach sitting in the same pane."""
        if self.sidecar is None or not self.store.get_setting("sidebar"):
            logger.debug(
                "sidecar preview skipped: sidecar_present=%s sidebar_setting=%s",
                self.sidecar is not None, self.store.get_setting("sidebar"),
            )
            return
        name = view.tmux_name if (view is not None and view.live) else None
        if name == self._preview_tmux_name:
            return
        logger.info("sidecar preview: %r -> %r", self._preview_tmux_name, name)
        self._preview_tmux_name = name
        if name is None:
            return
        try:
            self.sidecar.preview(nested_attach_command(self.tmux.socket, name, read_only=True))
        except Exception as error:
            logger.exception("live preview failed for %r", name)
            self.notify(f"Live preview failed: {error}", severity="warning")

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
            recent = self._recently_started.get(view.session_id)
            if recent and self.tmux.has_session(recent):
                # We ourselves just created/resumed this one; the registry
                # snapshot (polled every REFRESH_SECONDS) hasn't caught up
                # to it being live yet. Reuse it rather than spawning
                # another `claude --resume` for the same session id —
                # without this guard, mashing enter before the next poll
                # spawns duplicate tmux sessions racing each other on the
                # same underlying Claude session (the observed "flickers
                # and doesn't open").
                logger.info(
                    "attach %s: reusing recently-started tmux session %r "
                    "(registry hasn't caught up yet)", view.session_id, recent,
                )
                self._attach_tmux_session(recent)
            elif view.live:
                self._attach_tmux_session(view.tmux_name)
            else:
                logger.info("attach: %s is dead, resuming", view.session_id)
                self._resume_dead_session(view)
        except Exception as error:  # loud, specific failure (spec §11)
            logger.exception("attach failed for %s", view.session_id)
            self.notify(f"Attach failed: {error}", severity="error", timeout=10)
        self.refresh_data()

    def _attach_tmux_session(self, name: str) -> None:
        """Hand off to the real CLI: full-terminal suspend normally, or the
        right-hand pane when running as a sidecar rail."""
        use_sidecar = self.sidecar is not None and self.store.get_setting("sidebar")
        logger.info(
            "attach %r: use_sidecar=%s (sidecar_present=%s sidebar_setting=%s)",
            name, use_sidecar, self.sidecar is not None, self.store.get_setting("sidebar"),
        )
        if use_sidecar:
            self.sidecar.open(nested_attach_command(self.tmux.socket, name))
            self._preview_tmux_name = name  # now interactive; the next preview tick leaves it alone
            if not getattr(self, "_sidecar_hint_shown", False):
                self._sidecar_hint_shown = True
                self.notify(
                    "Opened in the right pane. Back to the list: ← (left arrow), "
                    "ctrl+\\, or click the rail.",
                    timeout=8,
                )
        else:
            self._suspend_and_run(lambda: self._fullscreen_attach(name))

    def _fullscreen_attach(self, name: str) -> None:
        """Classic attach, with left-arrow capture when enabled: Left
        detaches our client (only ours — tty-filtered) and cagents resumes.
        Runs inside App.suspend(), so stdin is the real terminal."""
        tty = self._current_tty() if self.store.get_setting("capture_left") else ""
        if not tty:
            self._statusline_attach(name)
            return
        try:
            self.tmux.bind_left_detach(tty)
        except Exception:
            self._statusline_attach(name)  # capture is best-effort, attach is not
            return
        try:
            self._statusline_attach(name)
        finally:
            try:
                self.tmux.unbind_left_detach()
            except Exception:
                pass  # stale binding is tty-filtered and harmless

    def _statusline_attach(self, name: str) -> None:
        """Attach with the cagents key hints on the session's statusline,
        restored to whatever it was afterwards."""
        shown = False
        try:
            self.tmux.session_statusline_on(name)
            shown = True
        except Exception:
            pass
        try:
            self.tmux.attach(name)
        finally:
            if shown:
                try:
                    self.tmux.session_statusline_off(name)
                except Exception:
                    pass

    @staticmethod
    def _current_tty() -> str:
        import sys

        try:
            return os.ttyname(sys.stdin.fileno())
        except OSError:
            return ""

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
        error = self.tmux.wait_for_alive_or_error(name)
        if error:
            logger.warning("resume of %s died immediately: %s", view.session_id, error)
            self.notify(f"Claude could not resume this session: {error}", severity="error", timeout=15)
            return
        self._recently_started[view.session_id] = name
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
        error = self.tmux.wait_for_alive_or_error(name)
        if error:
            logger.warning("new session %s died immediately: %s", session_id, error)
            self.notify(f"Claude could not start: {error}", severity="error", timeout=15)
            return
        self.store.track(session_id, directory, utcnow().isoformat(), label=label)
        pending = getattr(self, "_pending_todo_link", None)
        if pending:
            self.store.link_todo_session(pending, session_id)
            self._pending_todo_link = None
        self.selected_session_id = session_id
        self._recently_started[session_id] = name
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

    # -- links ---------------------------------------------------------------

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

    # -- fork ---------------------------------------------------------------------

    def action_fork(self) -> None:
        view = self.selected_view()
        if view is None or view.missing or view.parsed is None:
            self.notify("Select a session with a transcript to fork.", severity="warning")
            return
        self.push_screen(
            InputModal(
                f"Fork '{view.title}' — first prompt for the new session",
                placeholder="what should the fork work on?",
            ),
            lambda prompt: self._fork_confirmed(view.session_id, prompt),
        )

    def _fork_confirmed(self, source_id: str, prompt: str | None) -> None:
        if not prompt or not prompt.strip():
            return
        prompt = prompt.strip()
        view = self.snapshot.by_id(source_id)
        if view is None:
            self.notify("Source session vanished.", severity="error")
            return
        claude_bin = self._claude_bin()
        if not claude_bin:
            self.notify("claude CLI not found.", severity="error")
            return
        new_id = str(uuid.uuid4())
        label = prompt[:60]
        try:
            name = self.tmux.new_claude_session(
                view.project_dir,
                ["--resume", source_id, "--fork-session", "--session-id", new_id],
                session_id=new_id,
                claude_bin=claude_bin,
            )
        except Exception as error:
            self.notify(f"Fork failed: {error}", severity="error", timeout=10)
            return
        # The fork is named after its prompt; the original is untouched.
        self.store.track(
            new_id, view.project_dir, utcnow().isoformat(), label=label,
            parent_id=source_id, relation="fork",
        )
        self.selected_session_id = new_id
        self._attach_tmux_session(name)
        self._send_prompt_later(name, prompt, "Forked — prompt sent to the new session.")
        self.refresh_data()

    @work(thread=True, group="send")
    def _send_prompt_later(self, tmux_name: str, text: str, success_note: str) -> None:
        import time

        time.sleep(4.0)  # let the CLI boot before pasting
        try:
            self.tmux.send_text(tmux_name, text)
        except Exception as error:
            self.call_from_thread(
                self.notify, f"Session started but message not delivered: {error}",
                severity="error", timeout=10,
            )
            return
        self.call_from_thread(self.notify, success_note)

    # -- handoff --------------------------------------------------------------------

    def action_handoff(self) -> None:
        view = self.selected_view()
        if view is None or view.missing or view.parsed is None:
            self.notify("Select a session with a transcript to hand off.", severity="warning")
            return
        self.push_screen(
            InputModal(
                f"Handoff '{view.title}' — what should the successor focus on?",
                placeholder="the new session's task",
            ),
            lambda prompt: self._handoff_confirmed(view.session_id, prompt),
        )

    def _handoff_confirmed(self, source_id: str, prompt: str | None) -> None:
        if not prompt or not prompt.strip():
            return
        self.notify("Asking the session to write its handoff spec… (can take a minute)", timeout=10)
        self._handoff_worker(source_id, prompt.strip())

    def _handoff_runner(self, source_id: str):
        # Summary turn runs on a throwaway FORK of the old session, so the
        # original transcript is never touched.
        return CliClaudeRunner(
            claude_bin=self._claude_bin(),
            extra_args=("--resume", source_id, "--fork-session"),
        )

    @work(thread=True, exclusive=True, group="handoff")
    def _handoff_worker(self, source_id: str, prompt: str) -> None:
        try:
            spec = self._handoff_runner(source_id).run(summary_prompt(prompt))
        except Exception as error:
            self.call_from_thread(
                self.notify, f"Handoff spec failed: {error}", severity="error", timeout=10
            )
            return
        if not spec.strip():
            self.call_from_thread(
                self.notify, "Handoff spec came back empty — aborting.", severity="error"
            )
            return
        self.call_from_thread(self._handoff_spec_ready, source_id, prompt, spec.strip())

    def _handoff_spec_ready(self, source_id: str, prompt: str, spec: str) -> None:
        view = self.snapshot.by_id(source_id)
        if view is None:
            self.notify("Source session vanished mid-handoff.", severity="error")
            return
        claude_bin = self._claude_bin()
        new_id = str(uuid.uuid4())
        try:
            name = self.tmux.new_claude_session(
                view.project_dir, ["--session-id", new_id],
                session_id=new_id, claude_bin=claude_bin,
            )
        except Exception as error:
            self.notify(f"Handoff session failed to start: {error}", severity="error", timeout=10)
            return
        self.store.track(
            new_id, view.project_dir, utcnow().isoformat(), label=prompt[:60],
            parent_id=source_id, relation="handoff",
        )
        # The predecessor is done — restore anytime with r.
        self.store.mark_reviewed(source_id, utcnow().isoformat())
        self.selected_session_id = new_id
        self._attach_tmux_session(name)
        self._send_prompt_later(
            name, first_message(spec, prompt),
            "Handed off — previous session marked done (r on it restores).",
        )
        self.refresh_data()

    # -- lineage --------------------------------------------------------------------

    def action_related(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        rows: list[tuple[str, str, str]] = []

        def title_of(sid: str) -> str:
            other = self.snapshot.by_id(sid)
            if other is not None:
                return other.title
            tracked = self.store.sessions.get(sid)
            return tracked.label if tracked and tracked.label else sid[:8]

        if view.parent_id:
            rows.append((view.parent_id, f"parent ({view.relation})", title_of(view.parent_id)))
        for sid in view.sibling_ids:
            rows.append((sid, "sibling", title_of(sid)))
        for sid in view.child_ids:
            other = self.snapshot.by_id(sid)
            relation = other.relation if other else "child"
            rows.append((sid, f"child ({relation})", title_of(sid)))
        if not rows:
            self.notify("No forks or handoffs related to this session.")
            return
        self.push_screen(RelatedModal(rows), self._related_chosen)

    def _related_chosen(self, session_id: str | None) -> None:
        if not session_id:
            return
        if self.snapshot.by_id(session_id) is None:
            self.notify("That session isn't in the current views (archived?).", severity="warning")
            return
        if self.active_view_id not in ("grouped", "queue"):
            self.action_switch_view("queue")
        from .views import SessionList

        session_list = self.query_one(f"#{self.active_view_id}-list", SessionList)
        for i in range(session_list.option_count):
            if session_list.get_option_at_index(i).id == session_id:
                session_list.highlighted = i
                break

    # -- plugins ---------------------------------------------------------------------

    def action_add_plugin(self) -> None:
        self.push_screen(
            InputModal(
                "New plugin — what keybind or automation do you want?",
                placeholder="e.g. ctrl+g opens the session's PR in the browser",
            ),
            self._plugin_requested,
        )

    def _plugin_requested(self, request: str | None) -> None:
        if not request or not request.strip():
            return
        request = request.strip()
        plugin_dir = self.store.path.parent / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        message = PLUGIN_GUIDE.format(plugin_dir=plugin_dir, request=request)

        # Reuse the live meta session if there is one; otherwise start it.
        meta_view = next(
            (v for v in self.snapshot.views if v.tracked.label == "meta" and v.live), None
        )
        if meta_view is not None:
            try:
                self.tmux.send_text(meta_view.tmux_name, f"New plugin request: {request}")
                self.notify("Sent to the meta session — attach to watch it build.")
                self.selected_session_id = meta_view.session_id
            except Exception as error:
                self.notify(f"Could not reach meta session: {error}", severity="error")
            return
        claude_bin = self._claude_bin()
        if not claude_bin:
            self.notify("claude CLI not found.", severity="error")
            return
        session_id = str(uuid.uuid4())
        try:
            name = self.tmux.new_claude_session(
                str(plugin_dir), ["--session-id", session_id],
                session_id=session_id, claude_bin=claude_bin,
            )
        except Exception as error:
            self.notify(f"Meta session failed to start: {error}", severity="error")
            return
        self.store.track(session_id, str(plugin_dir), utcnow().isoformat(), label="meta")
        self.selected_session_id = session_id
        self._attach_tmux_session(name)
        self._send_prompt_later(
            name, message, "Meta session building your plugin — it hot-loads when saved."
        )
        self.refresh_data()

    # -- monitoring ------------------------------------------------------------------

    def action_toggle_monitoring(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        if view.state == SessionState.MONITORING:
            self.store.set_monitoring(view.session_id, "")
            self.notify("Monitoring cleared — back to 'needs review'.")
        elif view.state in (SessionState.NEEDS_REVIEW, SessionState.DONE, SessionState.STOPPED):
            self.store.set_monitoring(view.session_id, utcnow().isoformat())
            self.notify("Monitoring — it re-alerts on new activity.")
        else:
            self.notify("Monitoring applies once Claude has finished.", severity="warning")
            return
        self.refresh_data()

    # -- pause / wake -------------------------------------------------------------------

    def action_pause_todo(self) -> None:
        todo = self.selected_todo()
        if todo is None:
            return
        if todo.paused:
            self.store.unpause_todo(todo.todo_id)
            self.notify("Unpaused.")
            self.refresh_data()
            return
        if todo.done:
            self.notify("It's done — nothing to pause.", severity="warning")
            return
        self.push_screen(PauseModal(todo.text), lambda r: self._pause_chosen(todo.todo_id, r))

    def _pause_chosen(self, todo_id: str, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        kind, value = result
        now_iso = utcnow().isoformat()
        if kind == "none":
            self.store.pause_todo(todo_id, paused_at=now_iso)
            self.notify("Paused until you unpause it.")
        elif kind == "timer":
            seconds = parse_duration(value) or 0.0
            self.store.pause_todo(todo_id, paused_at=now_iso, wake_at=iso_in(seconds))
            self.notify(f"Paused — wakes in {value.strip()}.")
        else:  # criteria -> ask Claude for a check script
            self.notify("Paused — asking Claude to write the wake check…")
            self.store.pause_todo(todo_id, paused_at=now_iso, wake_criteria=value)
            self._generate_wake_script(todo_id, value)
        self.refresh_data()

    @work(thread=True, exclusive=True, group="wakegen")
    def _generate_wake_script(self, todo_id: str, criteria: str) -> None:
        todo = self.store.todos.get(todo_id)
        project = todo.project_dir if todo else ""
        runner = self.claude_runner or CliClaudeRunner(claude_bin=self._claude_bin())
        try:
            reply = runner.run(build_wake_prompt(criteria, project))
            script = extract_script(reply)
        except Exception as error:
            self.call_from_thread(
                self.notify,
                f"Wake script failed ({error}) — todo stays paused, wake it manually.",
                severity="warning", timeout=10,
            )
            return
        self.call_from_thread(
            self.push_screen,
            ScriptConfirmModal(criteria, script),
            lambda yes: self._wake_script_confirmed(todo_id, criteria, script, bool(yes)),
        )

    def _wake_script_confirmed(self, todo_id: str, criteria: str, script: str, yes: bool) -> None:
        if not yes:
            self.notify("Paused without a check — wake it manually (p).")
            return
        wake_dir = self.store.path.parent / "wake"
        wake_dir.mkdir(parents=True, exist_ok=True)
        script_path = wake_dir / f"{todo_id}.sh"
        script_path.write_text(script, "utf-8")
        script_path.chmod(0o755)
        todo = self.store.todos.get(todo_id)
        if todo is not None:
            self.store.pause_todo(
                todo_id, paused_at=todo.paused_at, wake_criteria=criteria,
                wake_script=str(script_path),
            )
        self.notify("Wake check saved — runs every ~5 minutes.")
        self.refresh_data()

    def _wake_tick(self) -> None:
        self._wake_tick_worker()

    @work(thread=True, exclusive=True, group="wake")
    def _wake_tick_worker(self) -> None:
        snapshot = self.snapshot

        def last_activity(todo) -> float | None:
            newest = None
            for sid in todo.session_ids:
                view = snapshot.by_id(sid)
                if view is not None and view.last_activity is not None:
                    ts = view.last_activity.timestamp()
                    newest = ts if newest is None else max(newest, ts)
            return newest

        report = self.wake_engine.tick(last_activity=last_activity)

        # Plugin automations ride the same clock (worker thread: slow ok).
        api = PluginAPI(self)
        for record in self.plugins.due_automations():
            try:
                record.tick(api)
            except Exception as error:
                record.error = f"tick: {error}"
                self.call_from_thread(
                    self.notify, f"Plugin '{record.name}' failed: {error}", severity="warning"
                )

        if not report.woken and not report.auto_paused:
            return
        self.call_from_thread(self._wake_report, report)

    def _wake_report(self, report) -> None:
        for todo_id, why in report.woken:
            todo = self.store.todos.get(todo_id)
            text = todo.text if todo else todo_id
            self.notify(f"Awake: {text} — {why}", timeout=10)
            if self.store.get_setting("desktop_notifications"):
                sid = todo.session_ids[-1] if todo and todo.session_ids else ""
                self.run_worker(
                    lambda t=text, w=why, s=sid: notify_desktop(
                        "cagents: todo awake", f"{t} — {w}", s, self.store.path.parent
                    ),
                    thread=True,
                )
        if report.auto_paused:
            self.notify(f"Auto-paused {len(report.auto_paused)} idle todo(s).")
        self.refresh_data()

    # -- shell / rich diff ------------------------------------------------------------

    def _target_dir(self) -> str:
        if self.active_view_id == "todos":
            todo = self.selected_todo()
            if todo is not None and (todo.worktree or todo.project_dir):
                return todo.worktree or todo.project_dir
        view = self.selected_view()
        return view.project_dir if view else ""

    def action_split_shell(self) -> None:
        directory = self._target_dir()
        if not directory or not Path(directory).is_dir():
            self.notify("No directory to open a shell in.", severity="warning")
            return
        if self.sidecar is not None:
            try:
                self.sidecar.split_shell(directory)
            except Exception as error:
                self.notify(f"Split failed: {error}", severity="error")
        else:
            shell = os.environ.get("SHELL", "/bin/zsh")
            self._suspend_and_run(lambda: __import__("subprocess").run([shell], cwd=directory))

    def action_rich_diff(self) -> None:
        """PR-style diff review in lazygit (commits panel = per-commit,
        files panel = the total working diff). Falls back to the built-in
        commentable diff screen when lazygit isn't installed."""
        directory = self._target_dir()
        if not directory or not Path(directory).is_dir():
            self.notify("No directory to diff.", severity="warning")
            return
        lazygit = shutil.which("lazygit")
        if not lazygit:
            self.notify("lazygit not installed — opening the built-in diff (D).")
            self.action_show_diff()
            return
        if self.sidecar is not None and self.store.get_setting("sidebar"):
            quoted = directory.replace("'", "'\\''")
            self.sidecar.open_command(f"{lazygit} -p '{quoted}'")
        else:
            self._suspend_and_run(
                lambda: __import__("subprocess").run([lazygit, "-p", directory])
            )

    # -- misc -------------------------------------------------------------------

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_expand_rail(self) -> None:
        """`=`: grow the sidecar rail back out. Mostly for running inside
        your own tmux, where the container's focus hooks don't exist."""
        if self.sidecar is None:
            return
        try:
            self.sidecar.expand()
        except Exception as error:
            self.notify(f"Could not resize: {error}", severity="warning")

    def action_settings(self) -> None:
        self.push_screen(SettingsModal(self.store, self._setting_changed))

    def _setting_changed(self, key: str, value: bool) -> None:
        if key == "capture_left" and os.environ.get("CAGENTS_SIDECAR") == "1":
            try:
                apply_left_capture(value)
            except Exception as error:
                self.notify(f"Could not apply Left binding: {error}", severity="error")
        # "sidebar" is consulted live on every attach; "notifications" gates
        # notify() directly — nothing else to do here.

    def action_help(self) -> None:
        self.push_screen(HelpModal())
