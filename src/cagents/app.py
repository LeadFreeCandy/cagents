"""The cagents application shell.

The interaction model (v2): cagents is a rail beside one viewer pane that
always shows the *real thing* — a live tmux attach of the highlighted
session, or its transcript when dead. Enter focuses the pane; ← cycles the
layout. Because preview and attach are the same mechanism, they cannot
disagree, and most "preview" features simply fall out.

Everything blocking runs in worker threads; the UI thread only ever
consumes immutable Snapshots (spec §4: the core loop stays instant).
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import ContentSwitcher, Footer, OptionList, Static

from . import gitops
from . import jira
from .claude_data import default_claude_dir, utcnow
from .ctx import CONTEXT_FILE, write_context
from .diffview import DiffResult, DiffScreen
from .format import header_summary, preview_renderable
from .handoff import first_message, summary_prompt
from .modals import (
    ConfirmModal,
    HelpModal,
    InputModal,
    PaletteModal,
    PlanConfirmModal,
    RelatedModal,
    SearchModal,
    SettingsModal,
    TrackModal,
)
from .notifier import notify_desktop, read_select_request
from .palette import CliClaudeRunner, apply_plan, build_prompt, parse_plan
from .sessions import SessionRegistry, SessionState, SessionView, Snapshot
from .sidecar import (
    CONTAINER_SOCKET,
    Sidecar,
    apply_ctx_binds,
    apply_dim_chat,
    apply_left_capture,
    nested_attach_command,
)
from .store import Store
from .tmuxctl import TmuxClient
from .views import GroupedView, KanbanView, QueueView, SelectionChanged

REFRESH_SECONDS = 2.0
# Viewer sync is leading-edge: a selection change attaches IMMEDIATELY (any
# delay reads as lag — user-tested both ways). Only follow-up changes inside
# this window coalesce into one trailing sync, so holding an arrow key down
# doesn't respawn the pane once per row.
VIEWER_COALESCE = 0.15
PR_POLL_SECONDS = 300.0
JIRA_POLL_SECONDS = 300.0
COMPACT_WIDTH = 60  # below this, the UI is a rail: no preview, dense rows

VIEW_IDS = ["queue", "grouped", "kanban"]

ALERT_STATES = (SessionState.NEEDS_INPUT, SessionState.NEEDS_REVIEW)


def _new_terminal_seed_command(directory: str, recents: list[str]) -> str:
    """The one compound shell command sent into a freshly-opened
    "new conversation" terminal: numbered cd-shortcuts to the most recent
    project directories, and a short printed menu. Convenience only — the
    shell is otherwise completely untouched; typing `claude` directly
    (with no alias involved) is already intercepted into a managed spawn
    by the claude shim on $PATH (see _write_claude_shim/_shim_env), the
    same mechanism the rest of cagents' terminal features use."""
    from .tmuxctl import _shquote

    commands = ["clear"]
    menu = [f"You're in: {directory}", ""]
    if recents:
        menu.append("Recent directories:")
        for i, recent in enumerate(recents, start=1):
            commands.append(f"alias {i}={_shquote(f'cd {_shquote(recent)}')}")
            menu.append(f"  {i}) {recent}")
        menu.append("")
        menu.append("Type a number to jump there, then run: claude")
    else:
        menu.append("Run: claude")
    commands.extend(f"echo {_shquote(line)}" for line in menu)
    return "; ".join(commands)


class CagentsApp(App):
    TITLE = "cagents"

    CSS = """
    #summary { height: 1; padding: 0 1; background: $panel; }
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
    #body.sidecar #preview-pane { display: none; }

    /* Toasts bottom-left instead of Textual's default bottom-right —
    the preview pane lives on the right, so a right-docked toast covers
    exactly what you're most likely looking at. */
    ToastRack { align: left bottom; }
    ToastHolder { align-horizontal: left; }
    """

    # Footer shows these in order; the important ones come first.
    BINDINGS = [
        Binding("enter", "attach", "Attach"),
        Binding("i", "enter_chat", "Enter chat", show=False),
        Binding("d", "toggle_done", "Done"),
        Binding("w", "toggle_waiting", "Waiting"),
        Binding("s", "toggle_snooze", "Snooze"),
        Binding("f", "fork", "Fork"),
        Binding("h", "handoff", "Handoff"),
        Binding("D", "show_diff", "Diff"),
        Binding("n", "new_session", "New"),
        Binding("N", "open_terminal", "Terminal", show=False),
        Binding("a", "track_session", "Track", show=False),
        Binding("1", "switch_view('queue')", "Queue", show=False),
        Binding("2", "switch_view('grouped')", "Grouped", show=False),
        Binding("3", "switch_view('kanban')", "Kanban", show=False),
        Binding("tab", "next_view", "Next view", show=False, priority=True),
        Binding("right", "grow_session", "Grow session", show=False),
        Binding("asterisk", "related", "Related", show=False),
        Binding("o", "open_link", "Open link", show=False),
        Binding("O", "open_jira", "Jira", show=False),
        Binding("R", "rename", "Rename", show=False),
        Binding("z", "undo", "Undo", show=False),
        Binding("x", "untrack", "Untrack", show=False),
        Binding("colon", "palette", "Fleet", show=False),
        Binding("slash", "search", "Search", show=False),
        Binding("R", "refresh_now", "Refresh", show=False),
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
        gh_runner=None,
        jira_fetch=None,
    ):
        super().__init__()
        self.store = store or Store.load()
        self.tmux = tmux or TmuxClient()
        self.claude_dir = claude_dir or default_claude_dir()
        self.registry = registry or SessionRegistry(
            self.store, tmux=self.tmux, claude_dir=self.claude_dir
        )
        self.sidecar = sidecar if sidecar is not None else (Sidecar() if Sidecar.enabled() else None)
        self.claude_runner = claude_runner  # lazy CliClaudeRunner if None
        self._pending_handoffs: dict[str, str] = {}  # source_id -> source title
        # Session ids `n` tracked before `claude` was ever typed in their
        # terminal — consumed by _handle_spawn_request the moment the shim
        # reports one, so that spawn reuses this exact id (already tracked,
        # already the list row waiting on it) instead of minting a new one.
        self._pending_new_terminals: set[str] = set()
        self.gh_runner = gh_runner  # injectable for the PR poller
        self.jira_fetch = jira_fetch  # injectable HTTP layer for the Jira poller
        self.snapshot = Snapshot()
        self.active_view_id = "queue"
        self.selected_session_id: str | None = None
        self.compact = False
        self._prev_states: dict[str, SessionState] = {}
        self._prev_last_activity: dict[str, object] = {}  # session_id -> last datetime seen
        self._seen_first_snapshot = False
        self._warned_no_notifier = False
        self._viewer_timer = None
        self._last_viewer_sync = 0.0
        import threading as _threading

        self._viewer_sync_lock = _threading.Lock()
        self._viewer_target: str = ""
        self._pending_highlight: str | None = None
        self._undo_stack: list[tuple[str, dict]] = []
        # Dead sessions the passive preview has already tried (silently)
        # to resume — at most once each, until the next snapshot reflects
        # the result. See _resume_for_preview.
        self._resumed_for_preview: set[str] = set()

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        with Horizontal(id="body"):
            with ContentSwitcher(initial="queue", id="views"):
                yield QueueView(id="queue")
                yield GroupedView(id="grouped")
                yield KanbanView(id="kanban")
            with VerticalScroll(id="preview-pane"):
                yield Static(id="preview-content")
        yield Footer()

    def on_mount(self) -> None:
        if self.sidecar is not None:
            # The right pane IS the preview; the internal one never shows.
            self.query_one("#body").add_class("sidecar")
        self._apply_compact(self.size.width)
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)
        self.set_interval(PR_POLL_SECONDS, self._poll_waiting_prs)
        self.set_interval(JIRA_POLL_SECONDS, self._poll_jira_prs)
        self.query_one("#queue", QueueView).focus_list()
        from . import ctx as _ctx

        _ctx.init_log(self.store.path.parent)
        _ctx._log(f"app started: {_ctx.version_stamp()} sidecar={self.sidecar is not None}")
        if os.environ.get("CAGENTS_SIDECAR") == "1" and self.sidecar is not None:
            try:
                apply_left_capture(bool(self.store.get_setting("capture_left")))
                apply_ctx_binds(self._ctx_prog(), str(self._context_path()))
                apply_dim_chat(bool(self.store.get_setting("dim_chat_preview")))
            except Exception as error:
                self.notify(f"Container key setup failed: {error}", severity="warning")
        try:
            self._write_claude_shim()
        except OSError as error:
            self.notify(f"claude shim not written: {error}", severity="warning")
        if self.sidecar is not None:
            try:
                self.sidecar.ensure_workspace(
                    os.environ.get("CAGENTS_LAUNCH_CWD") or os.getcwd(),
                    ctx_prog=self._ctx_prog(),
                    context_path=str(self._context_path()),
                    shim_env=self._shim_env(),
                )
            except Exception as error:
                self.notify(f"Workspace setup failed: {error}", severity="error")

    def action_quit(self) -> None:
        """`q` from inside the container must actually return the user to
        their real shell.

        Textual's default action_quit just exits this process — but this
        process is itself pane 0 of the container session. Exiting it
        alone kills only that pane; the container session (CONTAINER_SOCKET)
        it lived in doesn't close with it, and tmux renumbers whatever
        pane/window is left into slot 0 instead. The user is left staring
        at an orphaned, app-less tmux session with no way to navigate it —
        exactly the "q just closes the left window and leaves me in a
        buggy state" bug. Tear down the container on the way out instead.

        The tabbed workspace (WORK_SOCKET) is deliberately NOT torn down —
        it's a separate, detached tmux server that survives independently
        of this process, and that's the whole point: terminal tabs (and
        whatever's running in them) persist across a cagents restart.
        ensure_workspace() re-attaches to it next launch, recreating only
        whatever structural tab actually died. Real claude sessions live
        on an entirely separate socket (cagents-sessions / claude) and are
        never touched here either way."""
        if os.environ.get("CAGENTS_SIDECAR") == "1":
            self._teardown_container()
        self.exit()

    def _teardown_container(self) -> None:
        import subprocess

        # Killing this ends this process too (it's pane 0 on this very
        # socket) — same as closing any terminal you're running in.
        subprocess.run(["tmux", "-L", CONTAINER_SOCKET, "kill-server"], capture_output=True)

    def notify(self, message, *, title="", severity="information", timeout=None, **kwargs):
        """Routine toasts are opt-in (settings). Warnings and errors always
        show — silent failure is the one unforgivable sin (spec §11)."""
        try:
            # Every toast lands in ctx.log — "it threw an error at startup"
            # must be reconstructable after the fact.
            self._dbg(f"notify[{severity}]: {message}")
        except Exception:
            pass
        if severity == "information" and not self.store.get_setting("notifications"):
            return None
        if timeout is None:
            return super().notify(message, title=title, severity=severity, **kwargs)
        return super().notify(message, title=title, severity=severity, timeout=timeout, **kwargs)

    def on_resize(self, event) -> None:
        self._apply_compact(event.size.width)

    def _apply_compact(self, width: int) -> None:
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
        from textual.screen import ModalScreen

        if isinstance(self.screen, ModalScreen):
            return action in ("focus_next", "focus_previous")
        return True

    # -- data flow -----------------------------------------------------------

    def refresh_data(self) -> None:
        self._refresh_worker()

    @work(thread=True, exclusive=True, group="refresh", exit_on_error=False)
    def _refresh_worker(self) -> None:
        # This runs unconditionally every REFRESH_SECONDS, from the very
        # first tick at boot — a single session with data that trips some
        # edge case here must never take the whole app down with it
        # (spec §11: silent failure is the one unforgivable sin, and an
        # uncaught worker exception is fatal to the whole app by default).
        try:
            snapshot = self.registry.refresh()
        except Exception as error:
            self.call_from_thread(self.notify, f"Refresh failed: {error}", severity="error")
            return
        self.call_from_thread(self.apply_snapshot, snapshot)

    def apply_snapshot(self, snapshot: Snapshot) -> None:
        # A stale, slower-finishing refresh can complete AFTER a newer one
        # already landed — Textual's "exclusive" worker cancellation can't
        # actually stop a running thread (only the wrapping task), so an
        # older _refresh_worker call can still call back in here later —
        # confirmed live: a poll's own store mutation got silently
        # reverted by an in-flight refresh that had started before it.
        # generated_at is stamped at the START of registry.refresh(), so
        # it orders correctly by when the read began, not by when the
        # (possibly slow) work happened to finish; discard anything older
        # than what's already showing.
        if snapshot.generated_at < self.snapshot.generated_at:
            return
        self.snapshot = snapshot
        self.query_one("#summary", Static).update(header_summary(snapshot.counts()))
        for view_id in VIEW_IDS:
            self.query_one(f"#{view_id}").update_snapshot(snapshot)
        if self._pending_highlight and snapshot.by_id(self._pending_highlight):
            # A session we just created/tracked: select it in the list so
            # the viewer previews it (selection follows the highlight).
            if self.active_view_id == "kanban":
                self.action_switch_view("queue")
            self._highlight_session(self._pending_highlight)
            self._pending_highlight = None
        self._update_preview()
        self._notify_transitions(snapshot)
        self._handle_select_request()
        self._handle_spawn_request()
        self._handle_toast_requests()

    def current_view(self):
        return self.query_one(f"#{self.active_view_id}")

    def selected_view(self) -> SessionView | None:
        if self.selected_session_id is None:
            return None
        return self.snapshot.by_id(self.selected_session_id)

    def on_selection_changed(self, event: SelectionChanged) -> None:
        if event.view_id != self.active_view_id:
            return
        self.selected_session_id = event.session_id
        self._update_preview()
        self._write_context()
        self._schedule_viewer_sync()

    # -- the viewer pane (sidecar) ---------------------------------------------

    def _schedule_viewer_sync(self) -> None:
        if self.sidecar is None:
            return
        import time as _time

        if self._viewer_timer is not None:
            self._viewer_timer.stop()
            self._viewer_timer = None
        if _time.monotonic() - self._last_viewer_sync >= VIEWER_COALESCE:
            self._sync_viewer()
        else:
            self._viewer_timer = self.set_timer(VIEWER_COALESCE, self._sync_viewer)

    def _viewer_command(self, view: SessionView) -> str:
        socket = view.tmux_socket or self.tmux.create_socket
        return nested_attach_command(socket, view.tmux_name)

    def _sync_viewer(self) -> None:
        if self.sidecar is None:
            return
        import time as _time

        self._last_viewer_sync = _time.monotonic()
        view = self.selected_view()
        if view is None:
            return
        if not view.live:
            # Never a fake/static rendering of a dead session — resume the
            # real CLI right then, lazily (only the one you've actually
            # settled on, via the debounce that got us here).
            self._resume_for_preview(view)
            return
        # The tmux round-trips run OFF the UI thread: doing them inline in
        # the event handler blocked Textual's redraw exactly while the ←
        # focus hook resized the rail — a visibly torn/doubled list frame
        # for as long as the subprocess calls took.
        #
        # exit_on_error=False is deliberate: Textual's default kills the
        # WHOLE APP if a worker's callable raises anything other than
        # CancelledError. This is a passive background convenience
        # (keep the session/terminal panes in step with the highlight) —
        # a bug in it, or a subprocess call failing in some way the
        # try/except below doesn't anticipate, must never be fatal.
        # Replicated live: a stuck/rejected sync here took the whole
        # process down, leaving nothing but the boot placeholder forever
        # (the app was simply gone — confirmed via `ps`).
        self.run_worker(
            lambda v=view: self._sync_viewer_blocking(v),
            thread=True, group="viewer-sync", exclusive=True, exit_on_error=False,
        )

    def _sync_viewer_blocking(self, view: SessionView) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        with self._viewer_sync_lock:  # exclusive= can't stop a running thread
            if worker.is_cancelled:
                # Superseded by a newer sync while queued on the lock —
                # its target is stale now; let the newer one act instead
                # of doing (and possibly erroring on) work nobody wants.
                return
            try:
                self._sync_terminal(view)
                command = self._viewer_command(view)
                if command == self._viewer_target:
                    return
                self.sidecar.show_viewer(command)
                self._viewer_target = command
            except Exception as error:
                if not worker.is_cancelled:
                    self.call_from_thread(
                        self.notify, f"Viewer failed: {error}", severity="error"
                    )

    def _sync_terminal(self, view: SessionView) -> None:
        """Keep the term-1 PANE pointed at the currently selected session
        even while you're not looking at that tab — mirrors _sync_viewer
        for the session tab. Without this, term-1 only ever updated when
        you explicitly opened it (N / a tab click), so just scrolling
        through the list while sitting on the terminal tab looked exactly
        like one shell shared by every session — it was never re-pointed
        until you re-triggered it. Silent on the expected edge cases (no
        worktree, not live) — action_open_terminal is the loud path for
        those; passively scrolling past a session without one shouldn't
        spam a toast."""
        if self.sidecar is None or not view.live or not view.tmux_name:
            return
        from .ctx import resolve_terminal_directory

        directory, kind, _warning = resolve_terminal_directory(view.work_dir)
        if kind == "":
            return
        try:
            socket = view.tmux_socket or self.tmux.create_socket
            self.tmux.ensure_session_window(view.tmux_name, "term", directory, socket=socket)
            group = self.tmux.ensure_window_view(view.tmux_name, "term", socket=socket)
            self.sidecar.sync_terminal_tab(nested_attach_command(socket, group))
        except Exception:
            pass

    def _resume_for_preview(self, view: SessionView) -> None:
        """Lazily resume a dead session's real CLI the moment you settle
        on it while browsing. Silent on the expected edge cases (running
        elsewhere, missing transcript, no project dir) — those get loud
        feedback from the explicit attach path (Enter) instead; passively
        scrolling past one shouldn't spam a toast. Tried at most once per
        session_id: once we've decided to attempt it, we wait for the
        next snapshot to reflect the result rather than retrying on every
        subsequent settle."""
        if view.session_id in self._resumed_for_preview:
            return
        self._resumed_for_preview.add(view.session_id)
        name, _reason, _severity = self._resume_target(view)
        if name is None:
            return
        self.refresh_data()  # so the list picks up "live" as soon as possible
        command = nested_attach_command(self.tmux.create_socket, name)
        try:
            self.sidecar.show_viewer(command)
            self._viewer_target = command
        except Exception as error:
            self.notify(f"Viewer failed: {error}", severity="error")

    def _update_preview(self) -> None:
        """The in-app preview — only rendered when there is no sidecar."""
        if self.sidecar is not None:
            return
        content = self.query_one("#preview-content", Static)
        view = self.selected_view()
        if view is None:
            content.update("")
            return
        pane = self.query_one("#preview-pane", VerticalScroll)
        width = max(40, pane.size.width - 2)
        content.update(preview_renderable(view, datetime.now(timezone.utc), width=width))
        pane.scroll_end(animate=False)

    def _context_path(self) -> Path:
        return self.store.path.parent / CONTEXT_FILE

    def _write_context(self) -> None:
        view = self.selected_view()
        if view is not None:
            write_context(
                self._context_path(), view.work_dir, view.session_id,
                diff_mode=str(self.store.get_setting("diff_mode")),
                shim_dir=str(self._shim_dir()),
                tmux_name=view.tmux_name if view.live else "",
                tmux_socket=view.tmux_socket,
            )

    @staticmethod
    def _ctx_prog() -> str:
        sibling = Path(sys.executable).parent / "cagents-ctx"
        if sibling.exists():
            return str(sibling)
        return shutil.which("cagents-ctx") or "cagents-ctx"

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

    def action_grow_session(self) -> None:
        """→ with the rail focused (and no view consuming it): WIDE -> SMALL —
        focus moves into the session, the rail collapses via the tmux hook."""
        if self.sidecar is not None:
            try:
                self.sidecar.focus_session()
            except Exception as error:
                self.notify(f"Layout failed: {error}", severity="warning")

    # -- attaching (the core of the core loop) ---------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.selected_session_id = event.option.id
            self.action_attach()

    def action_attach(self) -> None:
        self._attach()

    def action_enter_chat(self) -> None:
        """i: attach AND zoom the chat to full width in one step — the
        vim "insert mode" feel this is modeled on (i = dive into the
        chat; ← already unwinds HIDDEN -> SMALL -> WIDE back to the
        list, so no new "exit" binding is needed for that half).

        Deliberately no Escape binding for the reverse direction: Escape
        is Claude's own interrupt key once a session pane has real
        focus, and a root-level tmux binding intercepts a key before the
        focused pane ever sees it — there's no way to tell "leave chat"
        and "interrupt Claude" apart for the same keypress there, so
        capturing it would break interrupting a running turn."""
        view = self._attach()
        if view is not None and self.sidecar is not None:
            try:
                self.sidecar.hide_rail()
            except Exception as error:
                self.notify(f"Layout failed: {error}", severity="warning")

    def _attach(self) -> SessionView | None:
        view = self.selected_view()
        if view is None:
            self.notify("No session selected.", severity="warning")
            return None
        if not self.tmux.available():
            self.notify("tmux not found on PATH — cannot attach.", severity="error")
            return None
        try:
            if view.live:
                self._attach_live(view)
            else:
                self._resume_dead_session(view)
        except Exception as error:  # loud, specific failure (spec §11)
            self.notify(f"Attach failed: {error}", severity="error", timeout=10)
            self.refresh_data()
            return None
        self.refresh_data()
        return view

    def _attach_live(self, view: SessionView) -> None:
        if self.sidecar is not None and self.store.get_setting("sidebar"):
            # Make sure the viewer shows this session, then just walk in.
            if self._viewer_timer is not None:
                self._viewer_timer.stop()
            command = self._viewer_command(view)
            if command != self._viewer_target:
                self.sidecar.show_viewer(command)
                self._viewer_target = command
            self.sidecar.focus_session()
        else:
            self._fullscreen_attach(view.tmux_name, view.tmux_socket or None)

    def _resume_dead_session(self, view: SessionView) -> None:
        """Explicit attach (Enter) on a dead session: resume its real
        `claude --resume` CLI for real and walk in. Loud on failure —
        this is a deliberate user action."""
        name, reason, severity = self._resume_target(view)
        if name is None:
            self.notify(reason, severity=severity, timeout=10)
            return
        self._show_new_session(name)

    def _resume_target(self, view: SessionView) -> tuple[str | None, str, str]:
        """Validate + spawn `claude --resume <id>` for a dead session.
        (tmux_name, "", "") on success, or (None, reason, severity) if it
        can't be resumed right now. Shared by the explicit attach path
        (loud on failure) and the passive-preview path (silent — see
        _resume_for_preview)."""
        if view.state == SessionState.WORKING:
            # Actively writing its transcript but hosted somewhere cagents
            # can't see (cmux, a bare terminal). Resuming would put a second
            # live CLI on one conversation — refuse.
            return None, (
                "This session is running outside cagents' tmux right now — "
                "attach from wherever it lives, or wait for it to finish."
            ), "warning"
        if view.missing:
            return None, (
                "This session's transcript is gone from Claude's store; nothing to resume."
            ), "error"
        directory = view.work_dir if Path(view.work_dir).is_dir() else view.project_dir
        if not Path(directory).is_dir():
            return None, f"Project directory no longer exists: {directory}", "error"
        claude_bin = self._claude_bin()
        if not claude_bin:
            return None, "claude CLI not found.", "error"
        name = self._spawn_session(directory, ["--resume", view.session_id], view.session_id)
        return name, "", ""

    def _show_new_session(self, tmux_name: str) -> None:
        """Point the viewer at a session we just created and walk in."""
        if self.sidecar is not None and self.store.get_setting("sidebar"):
            command = nested_attach_command(self.tmux.create_socket, tmux_name)
            self.sidecar.show_viewer(command)
            self._viewer_target = command
            self.sidecar.focus_session()
        else:
            self._fullscreen_attach(tmux_name, self.tmux.create_socket)

    def _fullscreen_attach(self, name: str, socket: str | None) -> None:
        """Classic whole-terminal attach, with the cagents statusline and the
        ← capture bound on that socket for the duration."""

        def run() -> None:
            tty = self._current_tty() if self.store.get_setting("capture_left") else ""
            bound_left = False
            if tty:
                try:
                    self.tmux.bind_left_detach(tty, socket=socket)
                    bound_left = True
                except Exception:
                    pass
            try:
                self.tmux.session_statusline_on(name, socket=socket)
                self.tmux.attach(name, socket=socket)
            finally:
                try:
                    self.tmux.session_statusline_off(name, socket=socket)
                    if bound_left:
                        self.tmux.unbind_left_detach(socket=socket)
                except Exception:
                    pass

        self._suspend_and_run(run)

    def _suspend_and_run(self, fn) -> None:
        from textual.app import SuspendNotSupported

        try:
            with self.suspend():
                fn()
        except SuspendNotSupported:
            fn()  # headless (tests): run without handing over the terminal

    def pick_directory_via_shell(self, start_dir: str, shell_cmd: list[str] | None = None) -> str:
        """Terminal-passthrough directory picker: hands the real terminal to
        the user's own interactive shell (aliases, zoxide, everything),
        seeded in start_dir. Wherever they are when they exit is the answer.

        A subprocess's cwd can't be read after it exits, so we poll it via
        lsof WHILE the shell runs and keep the last value — shell-agnostic,
        no rc-file tricks."""
        import subprocess
        import time

        shell_cmd = shell_cmd or [os.environ.get("SHELL", "/bin/zsh"), "-i"]
        seed = start_dir if Path(start_dir).is_dir() else str(Path.home())
        result = {"dir": seed}

        def run() -> None:
            proc = subprocess.Popen(shell_cmd, cwd=seed)
            while proc.poll() is None:
                out = subprocess.run(
                    ["lsof", "-a", "-p", str(proc.pid), "-d", "cwd", "-Fn"],
                    capture_output=True, text=True,
                )
                for line in out.stdout.splitlines():
                    if line.startswith("n/"):
                        result["dir"] = line[1:]
                time.sleep(0.2)

        self._suspend_and_run(run)
        picked = result["dir"]
        return picked if Path(picked).is_dir() else seed

    @staticmethod
    def _current_tty() -> str:
        try:
            return os.ttyname(sys.stdin.fileno())
        except OSError:
            return ""

    def _events_dir(self) -> Path:
        return self.store.path.parent / "events"

    def _spawn_request_path(self) -> Path:
        return self.store.path.parent / "spawn-request.json"

    def _shim_dir(self) -> Path:
        return self.store.path.parent / "bin"

    def _write_claude_shim(self) -> None:
        """`claude` typed in a cagents shell becomes a managed session: the
        shim files a spawn request that the app picks up within a refresh
        (~2s), spawning it properly — tracked, hooked, auto-selected, session
        tab focused. If cagents doesn't answer, it falls back to the real
        claude so the shell never dead-ends."""
        real = self._claude_bin() or "claude"
        request = self._spawn_request_path()
        shim = self._shim_dir() / "claude"
        script = f"""#!/bin/bash
# cagents shim — `claude` here opens a managed session in cagents.
REQUEST={str(request)!r}
python3 -c 'import json,sys; print(json.dumps({{"dir": sys.argv[1], "pending_id": sys.argv[2], "args": sys.argv[3:]}}))' \
  "$PWD" "$CAGENTS_SESSION_ID" "$@" > "$REQUEST.tmp" && mv "$REQUEST.tmp" "$REQUEST"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 0.4
  [ ! -e "$REQUEST" ] && {{ echo "opened in cagents → session tab"; exit 0; }}
done
rm -f "$REQUEST"
echo "cagents not responding — running claude directly"
exec {real!r} "$@"
"""
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(script, "utf-8")
        shim.chmod(0o755)
        self._write_zdot(shim)

    def _write_zdot(self, shim: Path) -> None:
        """The scoped `claude` override for zsh: rc files run AFTER any
        environment we inject and typically prepend their own PATH entries
        (defeating a plain PATH shim), so a ZDOTDIR sources the real config
        and then defines `claude` as a function. Applies ONLY to shells
        cagents creates — regular terminals are untouched."""
        zdot = self.store.path.parent / "zdot"
        zdot.mkdir(parents=True, exist_ok=True)
        (zdot / ".zshenv").write_text(
            '[ -f "$HOME/.zshenv" ] && source "$HOME/.zshenv"\n', "utf-8"
        )
        (zdot / ".zprofile").write_text(
            '[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile"\n', "utf-8"
        )
        (zdot / ".zshrc").write_text(
            '[ -f "$HOME/.zshrc" ] && source "$HOME/.zshrc"\n'
            "unalias claude 2>/dev/null\n"
            f'claude() {{ "{shim}" "$@" }}\n', "utf-8"
        )

    def _shim_env(self) -> list[str]:
        return [
            "-e", f"PATH={self._shim_dir()}:{os.environ.get('PATH', '')}",
            "-e", f"ZDOTDIR={self.store.path.parent / 'zdot'}",
        ]

    def _handle_spawn_request(self) -> None:
        """A cagents shell typed `claude` — spawn it for real."""
        import json as _json

        request = self._spawn_request_path()
        try:
            payload = _json.loads(request.read_text("utf-8"))
        except (OSError, _json.JSONDecodeError):
            return
        try:
            request.unlink()
        except OSError:
            pass
        directory = str(payload.get("dir", ""))
        pending_id = str(payload.get("pending_id", ""))
        args = [str(a) for a in payload.get("args", [])]
        if not directory or not Path(directory).is_dir():
            self.notify(f"Shell claude: bad directory {directory!r}", severity="error")
            return
        # An explicit resume keeps its id. Otherwise: if this shell is one
        # `n` opened (tagged with CAGENTS_SESSION_ID, still pending — the
        # EXISTING 'N' terminal-tab-on-a-live-session flow also carries
        # that env var, but for an already-spawned session, so only a
        # still-pending id is ever reused here), that id was tracked the
        # moment the terminal opened and the list row is waiting on it —
        # reuse it rather than minting a second, orphaned one. Anything
        # else gets a genuinely fresh id.
        session_id = ""
        if "--resume" in args:
            candidate = args[args.index("--resume") + 1] if args.index("--resume") + 1 < len(args) else ""
            if len(candidate) == 36:
                session_id = candidate
        if not session_id:
            if pending_id in self._pending_new_terminals:
                session_id = pending_id
                self._pending_new_terminals.discard(pending_id)
            else:
                session_id = str(uuid.uuid4())
            args = args + ["--session-id", session_id]
        try:
            name = self._spawn_session(directory, args, session_id)
        except Exception as error:
            self.notify(f"Shell claude failed: {error}", severity="error", timeout=10)
            return
        self._checkpoint("new session")
        self.store.track(session_id, directory, utcnow().isoformat())
        self.selected_session_id = session_id
        self._pending_highlight = session_id
        self._show_new_session(name)
        self.notify("Session opened from the shell.")
        self.refresh_data()

    def action_open_terminal(self) -> None:
        """N: give me the shell — the terminal tab, focused. cd around,
        type `claude`, and it lands back here.

        Each session gets its OWN persistent terminal (a second window
        inside that session's own tmux session, viewed through a grouped
        session so it doesn't drag the live claude pane's view along) —
        never one shell shared across every session in the app. Same
        resolution ctx.py's do_shell uses for a tab click (see
        resolve_terminal_directory) so a keypress and a click agree:
          - a genuine linked worktree -> open there, no fuss.
          - the shared repo checkout (no dedicated worktree for this
            session) -> still opens, but with a loud warning.
          - neither -> an explicit error, never a generic shell elsewhere.
        Re-derived fresh from the live view every time this runs (not
        cached), so a stale mapping from before a cagents restart is
        always rechecked and reassigned to whatever is actually live now."""
        from .ctx import resolve_terminal_directory

        view = self.selected_view()
        if view is None:
            self.notify("No session selected.", severity="error")
            return
        directory, kind, warning = resolve_terminal_directory(view.work_dir)
        if kind == "":
            # quiet by design (user choice): nothing usable to attach — the
            # C-t/tab path shows the placeholder; N just logs and stays put
            self._dbg(f"open_terminal: no worktree for {view.work_dir!r}")
            return
        if warning:
            self._dbg(f"open_terminal: {warning}")
        if self.sidecar is not None and self.store.get_setting("sidebar"):
            try:
                if view.live and view.tmux_name:
                    socket = view.tmux_socket or self.tmux.create_socket
                    self.tmux.ensure_session_window(
                        view.tmux_name, "term", directory, socket=socket
                    )
                    group = self.tmux.ensure_window_view(
                        view.tmux_name, "term", socket=socket, force_select=True
                    )
                    command = nested_attach_command(socket, group)
                else:
                    import shlex

                    shell = os.environ.get("SHELL", "/bin/zsh")
                    command = f"cd {shlex.quote(directory)} && exec {shlex.quote(shell)}"
                self.sidecar.open_terminal_tab(command)
                self.sidecar.focus_pane()
            except Exception as error:
                self.notify(f"Terminal failed: {error}", severity="error")
        else:
            shell = os.environ.get("SHELL", "/bin/zsh")
            env = os.environ.copy()
            env["PATH"] = f"{self._shim_dir()}:{env.get('PATH', '')}"
            env["ZDOTDIR"] = str(self.store.path.parent / "zdot")
            self._suspend_and_run(
                lambda: __import__("subprocess").run([shell, "-i"], cwd=directory, env=env)
            )

    def _hook_args(self, session_id: str) -> list[str]:
        """--settings JSON wiring Claude Code's own hooks to stamp this
        session's state events — the authoritative alternative to pane
        heuristics (Notification = needs you, Stop = turn over,
        UserPromptSubmit = working)."""
        import json as _json

        events_file = self._events_dir() / f"{session_id}.json"
        prog = self._ctx_prog()

        def hook(kind: str) -> list:
            return [{"hooks": [{"type": "command",
                                "command": f"{prog} event {kind} --file {events_file}"}]}]

        settings = {"hooks": {k: hook(k) for k in ("Notification", "Stop", "UserPromptSubmit")}}
        return ["--settings", _json.dumps(settings)]

    def _spawn_session(self, directory: str, claude_args: list[str], session_id: str) -> str:
        """Every session cagents starts goes through here: private socket,
        state hooks attached."""
        return self.tmux.new_claude_session(
            directory, claude_args + self._hook_args(session_id),
            session_id=session_id, claude_bin=self._claude_bin(),
        )

    def _claude_bin(self) -> str:
        found = shutil.which("claude")
        if found:
            return found
        fallback = Path.home() / ".local" / "bin" / "claude"
        return str(fallback) if fallback.exists() else ""

    # -- undo ---------------------------------------------------------------------

    def _checkpoint(self, label: str) -> None:
        """Snapshot the session bookkeeping before a mutation, so `z` can
        take it back. Only cagents' own state — never Claude's data, and
        never processes (undoing a fork untracks it; the session lives on)."""
        self._undo_stack.append((label, self.store.export_sessions()))
        del self._undo_stack[:-20]

    def action_undo(self) -> None:
        if not self._undo_stack:
            self.notify("Nothing to undo.", severity="warning")
            return
        label, payload = self._undo_stack.pop()
        self.store.restore_sessions(payload)
        self.notify(f"Undid: {label}")
        self.refresh_data()

    def _notify_undoable(self, message: str, *, severity: str = "information") -> None:
        """A routine toast for a `z`-undoable mutation — clicking the toast
        itself undoes it (same action as pressing z), not just dismisses
        it. Only meaningful right after a _checkpoint(); the undo stack is
        a single shared LIFO, so clicking an older toast undoes whatever
        is *currently* on top, same as `z` always has — acceptable since
        toasts are short-lived."""
        self.notify(f"{message} [@click=app.undo]click to undo[/]", severity=severity)

    # -- done / waiting ---------------------------------------------------------

    def _pin_cursor(self) -> None:
        """Tell every list view to keep the cursor at its current ROW
        INDEX on the very next render, instead of chasing the selected
        session's id to wherever it moves — used right before an action
        that shifts the current row elsewhere in the same list (mark
        done, snooze, ...), so the cursor lands on whatever now sits in
        the old spot rather than following the row down."""
        for view_id in VIEW_IDS:
            self.query_one(f"#{view_id}").pin_cursor_position()

    def _optimistic_state(self, session_id: str, state: SessionState, detail: str) -> None:
        """Render a state change the user just caused immediately, on this
        same keystroke, instead of waiting for the next full background
        refresh (registry.refresh() re-parses every session's transcript
        and re-captures every tmux pane — a real refresh_data() call right
        after this one still runs and reconciles everything properly; this
        is purely a same-frame visual echo so the row doesn't sit stale for
        the second or so that takes)."""
        from .sessions import attention_rank_map

        view = self.snapshot.by_id(session_id)
        if view is None:
            return
        view.state = state
        view.state_detail = detail
        if self.store.get_setting("time_ordered_queue"):
            view.attention_rank = 0
        else:
            view.attention_rank = attention_rank_map(self.store.get_setting("state_order"))[state]
        for view_id in VIEW_IDS:
            self.query_one(f"#{view_id}").update_snapshot(self.snapshot)

    def action_toggle_done(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        self._checkpoint("done change")
        if view.state == SessionState.DONE:
            self.store.clear_reviewed(view.session_id)
            self._notify_undoable("Un-done — back in the queue.")
            self._pin_cursor()
            self._optimistic_state(view.session_id, SessionState.NEEDS_REVIEW, "finished, unreviewed")
        elif view.state in (SessionState.WORKING, SessionState.NEEDS_INPUT):
            self.notify("Still in flight — mark it done when Claude is finished.", severity="warning")
            return
        else:
            self.store.mark_reviewed(view.session_id, utcnow().isoformat())
            self._notify_undoable("Done.")
            self._pin_cursor()
            self._optimistic_state(view.session_id, SessionState.DONE, "done")
        self.refresh_data()

    def action_toggle_waiting(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        if view.state == SessionState.WAITING_EXTERNAL:
            self._checkpoint("waiting change")
            self.store.clear_waiting(view.session_id)
            self._notify_undoable("No longer waiting on the PR.")
            self.refresh_data()
            return
        if view.state in (SessionState.WORKING, SessionState.NEEDS_INPUT):
            self.notify("Still in flight — park it once Claude is finished.", severity="warning")
            return
        # Prefer a PR the session itself recorded; else the manual
        # association; else look one up; else ask.
        recorded = self._recorded_pr_url(view)
        if recorded:
            self._set_waiting(view.session_id, recorded)
        else:
            branch = view.parsed.git_branch if view.parsed else ""
            self._find_pr_worker(view.session_id, view.project_dir, branch)

    def action_toggle_snooze(self) -> None:
        """s: defer this session for `snooze_duration` (settings panel;
        default 1h) — a genuinely blocking/active session (WORKING,
        NEEDS_INPUT) can't be snoozed away, same guard as done/waiting."""
        view = self.selected_view()
        if view is None:
            return
        if view.state == SessionState.SNOOZED:
            self._checkpoint("snooze change")
            self.store.clear_snooze(view.session_id)
            self._notify_undoable("Un-snoozed — back in the queue.")
            self.refresh_data()
            return
        if view.state in (SessionState.WORKING, SessionState.NEEDS_INPUT):
            self.notify("Still in flight — snooze it once Claude is finished.", severity="warning")
            return
        from datetime import timedelta

        from .store import SNOOZE_MINUTES

        duration = str(self.store.get_setting("snooze_duration"))
        minutes = SNOOZE_MINUTES.get(duration, 60)
        self._checkpoint("snooze change")
        until = utcnow() + timedelta(minutes=minutes)
        self.store.set_snooze(view.session_id, until.isoformat())
        self._notify_undoable(f"Snoozed for {duration} (until {until.strftime('%H:%M')}).")
        self.refresh_data()

    @staticmethod
    def _recorded_pr_url(view: SessionView) -> str:
        """The PR this session is actually tied to: what Claude itself
        recorded in the transcript, else a manual/waiting association.
        Never re-derived from "whatever branch is checked out now"."""
        if view.parsed:
            for link in reversed(view.parsed.links):
                if link.kind == "pr" and link.url:
                    return link.url
        return view.tracked.pr_url or view.tracked.waiting_pr

    @work(thread=True, exclusive=True, group="findpr", exit_on_error=False)
    def _find_pr_worker(self, session_id: str, directory: str, expected_branch: str = "") -> None:
        url = gitops.find_pr_url(directory, runner=self.gh_runner, expected_branch=expected_branch)
        if url:
            self.call_from_thread(self._set_waiting, session_id, url)
        else:
            self.call_from_thread(
                self.push_screen,
                InputModal(
                    "No PR found for this branch — paste the PR URL",
                    placeholder="https://github.com/owner/repo/pull/123",
                ),
                lambda text: self._waiting_pr_entered(session_id, text),
            )

    def _waiting_pr_entered(self, session_id: str, text: str | None) -> None:
        if not text or not text.strip():
            return
        self.store.set_pr_url(session_id, text.strip())  # remember for o / next time
        self._set_waiting(session_id, text.strip())

    def _set_waiting(self, session_id: str, pr_url: str) -> None:
        self._checkpoint("waiting change")
        self.store.set_waiting(session_id, utcnow().isoformat(), pr_url)
        self._notify_undoable(f"Waiting on {pr_url} — comments re-alert; merge marks it done.")
        self.refresh_data()

    def _poll_waiting_prs(self) -> None:
        self._poll_waiting_worker()

    @work(thread=True, exclusive=True, group="prpoll", exit_on_error=False)
    def _poll_waiting_worker(self) -> None:
        changed = False
        own_login = gitops.current_github_login(runner=self.gh_runner)
        for tracked in list(self.store.sessions.values()):
            if not tracked.waiting_since or not tracked.waiting_pr:
                continue
            try:
                status = gitops.pr_status(
                    tracked.waiting_pr, runner=self.gh_runner, own_login=own_login
                )
            except Exception:
                continue  # transient gh failure; try again next poll
            short = tracked.label or tracked.session_id[:8]
            if status.merged:
                self.store.mark_reviewed(tracked.session_id, utcnow().isoformat())
                self.store.clear_waiting(tracked.session_id, finished_reason="merged")
                changed = True
                self._desktop_note(f"PR merged — {short} is done", tracked.session_id)
            elif status.closed:
                # Closed without merging: waiting would never resolve — re-alert.
                self.store.clear_waiting(tracked.session_id, finished_reason="pr closed")
                changed = True
                self._desktop_note(f"PR closed — {short} needs review", tracked.session_id)
            else:
                note, reason = self._external_update_reason(status, tracked.waiting_since)
                if reason:
                    self.store.mark_external_update(tracked.session_id, utcnow().isoformat(), reason)
                    changed = True
                    self._desktop_note(f"{note} — {short} external update", tracked.session_id)
        if changed:
            self.call_from_thread(self.refresh_data)

    def _external_update_reason(self, status, waiting_since: str) -> tuple[str, str]:
        """Which (if any) enabled trigger category fired since the
        session parked as waiting — each independently toggleable in
        settings (see SETTINGS_DEFAULTS). Checked in a fixed priority so
        one poll reports exactly one reason even if several categories
        moved at once, rather than double-notifying."""
        get = self.store.get_setting
        if get("external_update_on_comments") and status.last_comment_from_others > waiting_since:
            return "New PR comments", "github comments"
        if get("external_update_on_self_comments") and status.last_comment_from_self > waiting_since:
            return "Your own PR comment", "own comment"
        if get("external_update_on_reviews") and status.last_review > waiting_since:
            return "New PR review", "pr review"
        if get("external_update_on_commits") and status.last_commit > waiting_since:
            return "New commits pushed", "new commits"
        if get("external_update_on_other_changes") and status.updated_at > waiting_since:
            # Anything else GitHub bumped updatedAt for (labels, title/
            # base edits, ...) not already covered above.
            return "PR updated", "pr updated"
        return "", ""

    def _desktop_note(self, message: str, session_id: str) -> None:
        """Called from background poll workers — dispatches the actual
        notification onto its OWN worker rather than running it inline,
        via call_from_thread since run_worker itself must be started from
        the main thread. Real bug, confirmed live: notify_desktop shells
        out to terminal-notifier / osascript with a 10s timeout, and this
        used to run synchronously INSIDE the poll worker's own thread,
        before that same worker's later refresh_data() call. A slow or
        hung notifier call (terminal-notifier can genuinely take the
        full 10s in some environments) blocked the poll from ever
        reaching the refresh that would make its own already-applied
        store mutation visible — the store was correct, but the UI kept
        showing stale state for up to 10s (or until the next unrelated
        refresh) every time. Desktop notification delivery has no
        business gating anything else."""
        if self.store.get_setting("desktop_notifications"):
            self.call_from_thread(self._dispatch_desktop_note, message, session_id)

    def _dispatch_desktop_note(self, message: str, session_id: str) -> None:
        self.run_worker(
            lambda: notify_desktop("cagents", message, session_id, self.store.path.parent),
            thread=True, exit_on_error=False,
        )

    # -- Jira (optional; jira_integration setting) ---------------------------

    def _poll_jira_prs(self) -> None:
        if not self.store.get_setting("jira_integration"):
            return
        self._poll_jira_worker(list(self.snapshot.views))

    @work(thread=True, exclusive=True, group="jirapoll", exit_on_error=False)
    def _poll_jira_worker(self, views: list[SessionView]) -> None:
        if not jira.credentials_configured():
            return
        changed = False
        for view in views:
            tracked = view.tracked
            # Re-derived fresh every poll, never trusted from cache — a
            # session's *recorded* PR can change after its jira_key was
            # first set (confirmed live: a session that incidentally
            # linked an unrelated PR early on, before its real one
            # existed, got that unrelated PR's ticket cached forever,
            # since the old code only ever derived the key once and then
            # just re-polled that same key's status). _recorded_pr_url
            # always prefers the session's real recorded PR over the
            # directory guess, so once a real PR exists this self-heals.
            #
            # expected_branch guards the OTHER half of this same bug class,
            # confirmed live: `view.project_dir` is often a SHARED checkout
            # (several sessions, no dedicated worktree each), and
            # find_pr_url otherwise answers "whatever's checked out right
            # now" — a different session's branch, a different PR, a
            # different Jira card, silently attached and then cached into
            # tracked.pr_url forever (defeating the self-heal above, since
            # _recorded_pr_url trusts that field once it's non-empty). The
            # session's own last-known branch (from its own transcript,
            # never from re-inspecting the directory) is what find_pr_url
            # verifies the found PR against before trusting it.
            pr_url = self._recorded_pr_url(view) or gitops.find_pr_url(
                view.project_dir, runner=self.gh_runner,
                expected_branch=view.parsed.git_branch if view.parsed else "",
            )
            if not pr_url:
                # No legitimate PR to derive from this poll (e.g.
                # find_pr_url now correctly refusing a shared, non-
                # dedicated checkout it used to trust) — clear rather
                # than leave a stale, unverifiable card sitting there;
                # derived, not stored.
                if tracked.jira_key:
                    self.store.clear_jira_info(tracked.session_id)
                    changed = True
                continue
            title, body, branch = gitops.pr_jira_sources(pr_url, runner=self.gh_runner)
            key = jira.extract_jira_key(title, body, branch)
            if not key:
                if tracked.jira_key:
                    self.store.clear_jira_info(tracked.session_id)
                    changed = True
                continue
            try:
                issue = jira.fetch_issue(key, fetch=self.jira_fetch)
            except jira.JiraError:
                continue  # transient Jira failure; try again next poll
            if (key, issue.status, issue.assignee) != (
                tracked.jira_key, tracked.jira_status, tracked.jira_assignee
            ):
                changed = True
            self.store.set_jira_info(
                tracked.session_id, key, issue.status, issue.assignee, utcnow().isoformat()
            )
        if changed:
            self.call_from_thread(self.refresh_data)

    # -- desktop notifications / click-select ------------------------------------

    def _dbg(self, message: str) -> None:
        """Verbose debug trace into ctx.log (same file the tmux hooks use,
        so user input, app behavior, and hook activity cross-validate on
        one timeline). Toggleable via the debug_log setting."""
        if self.store.get_setting("debug_log"):
            from .ctx import _log

            _log(f"app: {message}")

    async def on_event(self, event) -> None:
        from textual import events

        if isinstance(event, events.Key):
            focused = type(self.focused).__name__ if self.focused else "-"
            self._dbg(f"key {event.key!r} focus={focused} screen={type(self.screen).__name__}")
        elif isinstance(event, (events.MouseDown, events.MouseUp)):
            kind = "mousedown" if isinstance(event, events.MouseDown) else "mouseup"
            self._dbg(f"{kind} ({event.x},{event.y}) button={event.button}")
        elif isinstance(event, events.Paste):
            self._dbg(f"paste ({len(event.text)} chars)")
        await super().on_event(event)

    def _notify_transitions(self, snapshot: Snapshot) -> None:
        first = not self._seen_first_snapshot
        self._seen_first_snapshot = True
        enabled = bool(self.store.get_setting("desktop_notifications"))
        if enabled and not self._warned_no_notifier:
            self._warned_no_notifier = True
            import shutil as _shutil

            if not _shutil.which("terminal-notifier"):
                self.notify(
                    "desktop notifications: terminal-notifier not installed — "
                    "falling back to osascript (unreliable, no click-to-select). "
                    "Fix: brew install terminal-notifier",
                    severity="warning",
                    timeout=12,
                )
        for view in snapshot.views:
            previous = self._prev_states.get(view.session_id)
            previous_last_activity = self._prev_last_activity.get(view.session_id)
            self._prev_states[view.session_id] = view.state
            self._prev_last_activity[view.session_id] = (
                view.parsed.last_timestamp if view.parsed else None
            )
            if previous is not None and previous != view.state:
                self._dbg(
                    f"state {view.session_id[:8]}: {previous.value} -> {view.state.value}"
                    f" ({view.state_detail})"
                )
                self._check_state_invariant(previous, previous_last_activity, view)
            if first or not enabled:
                continue
            if view.state in ALERT_STATES and previous not in ALERT_STATES and previous is not None:
                self.run_worker(
                    lambda v=view: notify_desktop(
                        f"cagents: {v.state.value}",
                        f"{v.title} — {v.needs_line or v.state_detail}",
                        v.session_id,
                        self.store.path.parent,
                    ),
                    thread=True, exit_on_error=False,
                )

    def _check_state_invariant(
        self, previous: SessionState, previous_last_activity, view: SessionView
    ) -> None:
        """Catch state-derivation bugs the moment they happen, in the wild,
        instead of only when someone notices a session showing the wrong
        label. See sessions.check_state_invariant / REQUIRES_NEW_ACTIVITY
        for exactly which transitions are watched and why. Always-on (not
        gated by the debug_log setting) and loud — this is meant to
        surface bugs, not get tuned into silence."""
        from .sessions import check_state_invariant

        violation = check_state_invariant(previous, previous_last_activity, view)
        if violation is None:
            return
        from .ctx import _log

        tracked = view.tracked
        _log(
            "STATE-INVARIANT-VIOLATION "
            f"session={view.session_id} title={view.title!r} {violation} | "
            f"live={view.live} tmux_name={view.tmux_name!r} "
            f"reviewed_at={tracked.reviewed_datetime()!r} "
            f"waiting_since={tracked.waiting_datetime()!r} "
            f"external_update={tracked.external_update_datetime()!r} "
            f"pending_tool_use={view.parsed.pending_tool_use if view.parsed else None} "
            f"last_record_role={view.parsed.last_record_role if view.parsed else None!r}"
        )
        self.notify(
            f"State invariant violation on '{view.title}': {violation} — see ctx.log",
            severity="error", timeout=15,
        )

    def _handle_toast_requests(self) -> None:
        """Warnings queued by cagents-ctx (the C-t / tab-click hooks run in
        their own short-lived processes and can't toast directly)."""
        import json

        from .ctx import TOAST_REQUEST_FILE

        path = self.store.path.parent / TOAST_REQUEST_FILE
        try:
            lines = path.read_text("utf-8").splitlines()
            path.unlink()
        except OSError:
            return
        for line in lines[-5:]:  # a stale backlog must not toast-storm
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = str(data.get("message", "")).strip()
            severity = data.get("severity", "warning")
            if message and severity in ("information", "warning", "error"):
                self._dbg(f"toast from ctx [{severity}]: {message}")
                self.notify(message, severity=severity)

    def _handle_select_request(self) -> None:
        session_id = read_select_request(self.store.path.parent)
        if not session_id or self.snapshot.by_id(session_id) is None:
            return
        self._dbg(f"select-request -> {session_id[:8]} (notification click)")
        if self.active_view_id == "kanban":
            self.action_switch_view("queue")
        self._highlight_session(session_id)

    def _highlight_session(self, session_id: str) -> None:
        from .views import SessionList

        session_list = self.query_one(f"#{self.active_view_id}-list", SessionList)
        for i in range(session_list.option_count):
            if session_list.get_option_at_index(i).id == session_id:
                session_list.highlighted = i
                return

    # -- fork / handoff / lineage --------------------------------------------------

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
        try:
            name = self._spawn_session(
                view.project_dir,
                ["--resume", source_id, "--fork-session", "--session-id", new_id],
                new_id,
            )
        except Exception as error:
            self.notify(f"Fork failed: {error}", severity="error", timeout=10)
            return
        self._checkpoint("fork")
        self.store.track(
            new_id, view.project_dir, utcnow().isoformat(), label=prompt[:60],
            parent_id=source_id, relation="fork",
        )
        self.selected_session_id = new_id
        self._pending_highlight = new_id
        self._show_new_session(name)
        self._send_prompt_later(name, prompt, "Forked — prompt sent to the new session.")
        self.refresh_data()

    @work(thread=True, group="send", exit_on_error=False)
    def _send_prompt_later(self, tmux_name: str, text: str, success_note: str) -> None:
        import time

        time.sleep(4.0)  # let the CLI boot before pasting
        try:
            self.tmux.send_text(tmux_name, text, socket=self.tmux.create_socket)
        except Exception as error:
            self.call_from_thread(
                self.notify, f"Session started but message not delivered: {error}",
                severity="error", timeout=10,
            )
            return
        self.call_from_thread(self.notify, success_note)

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
        # Show the successor-to-be in the list immediately: the spec turn can
        # take a minute and otherwise nothing visibly happens.
        view = self.snapshot.by_id(source_id)
        title = view.title if view is not None else source_id[:8]
        self._pending_handoffs[source_id] = title
        self._push_pending_rows()
        self.notify("Asking the session to write its handoff spec… (can take a minute)", timeout=10)
        self._handoff_worker(source_id, prompt.strip())

    def _push_pending_rows(self) -> None:
        rows = [
            (f"pending-handoff-{source_id}", f"creating handoff from: {title}")
            for source_id, title in self._pending_handoffs.items()
        ]
        for view_id in VIEW_IDS:
            view = self.query_one(f"#{view_id}")
            view.pending_rows = rows
            view.update_snapshot(self.snapshot)

    def _clear_pending_handoff(self, source_id: str) -> None:
        if self._pending_handoffs.pop(source_id, None) is not None:
            self._push_pending_rows()

    def _handoff_failed(self, source_id: str, message: str) -> None:
        self._clear_pending_handoff(source_id)
        self.notify(message, severity="error", timeout=10)

    def _handoff_runner(self, source_id: str):
        # Summary turn runs on a throwaway FORK of the old session, so the
        # original transcript is never touched.
        return CliClaudeRunner(
            claude_bin=self._claude_bin(),
            extra_args=("--resume", source_id, "--fork-session"),
        )

    @work(thread=True, exclusive=True, group="handoff", exit_on_error=False)
    def _handoff_worker(self, source_id: str, prompt: str) -> None:
        try:
            spec = self._handoff_runner(source_id).run(summary_prompt(prompt))
        except Exception as error:
            self.call_from_thread(
                self._handoff_failed, source_id, f"Handoff spec failed: {error}"
            )
            return
        if not spec.strip():
            self.call_from_thread(
                self._handoff_failed, source_id, "Handoff spec came back empty — aborting."
            )
            return
        self.call_from_thread(self._handoff_spec_ready, source_id, prompt, spec.strip())

    def _handoff_spec_ready(self, source_id: str, prompt: str, spec: str) -> None:
        self._clear_pending_handoff(source_id)
        view = self.snapshot.by_id(source_id)
        if view is None:
            self.notify("Source session vanished mid-handoff.", severity="error")
            return
        new_id = str(uuid.uuid4())
        try:
            name = self._spawn_session(view.project_dir, ["--session-id", new_id], new_id)
        except Exception as error:
            self.notify(f"Handoff session failed to start: {error}", severity="error", timeout=10)
            return
        self._checkpoint("handoff")
        self.store.track(
            new_id, view.project_dir, utcnow().isoformat(), label=prompt[:60],
            parent_id=source_id, relation="handoff",
        )
        # The predecessor is done — restore anytime with d.
        self.store.mark_reviewed(source_id, utcnow().isoformat())
        self.selected_session_id = new_id
        self._pending_highlight = new_id
        self._show_new_session(name)
        self._send_prompt_later(
            name, first_message(spec, prompt),
            "Handed off — previous session marked done (d on it restores).",
        )
        self.refresh_data()

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
        if self.active_view_id == "kanban":
            self.action_switch_view("queue")
        self._highlight_session(session_id)

    # -- diff review ------------------------------------------------------------------

    def action_show_diff(self) -> None:
        view = self.selected_view()
        if view is None:
            self.notify("Nothing selected to diff.", severity="warning")
            return
        self.notify("Building diff…")
        self._diff_worker(view.work_dir)

    @work(thread=True, exclusive=True, group="diff", exit_on_error=False)
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
            diff, target_desc=target,
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

    @work(thread=True, exclusive=True, group="sendreview", exit_on_error=False)
    def _send_review_worker(self, view: SessionView, message: str, count: int) -> None:
        import time

        try:
            if view.live:
                tmux_name, socket = view.tmux_name, (view.tmux_socket or None)
            else:
                tmux_name = self._spawn_session(
                    view.project_dir, ["--resume", view.session_id], view.session_id
                )
                socket = self.tmux.create_socket
                time.sleep(4.0)
            self.tmux.send_text(tmux_name, message, socket=socket)
        except Exception as error:
            self.call_from_thread(
                self.notify, f"Could not deliver comments: {error}",
                severity="error", timeout=10,
            )
            return
        self.call_from_thread(
            self.notify, f"Sent {count} review comment{'s' if count != 1 else ''} to Claude."
        )
        self.call_from_thread(self.refresh_data)

    # -- creating / tracking sessions ------------------------------------------

    def _recent_directories(self, limit: int = 5) -> list[str]:
        """Distinct project directories from the most recently tracked
        sessions, newest first — offered as quick-jump shortcuts in the
        new-conversation terminal."""
        ordered = sorted(self.store.sessions.values(), key=lambda t: t.added_at, reverse=True)
        seen: list[str] = []
        for tracked in ordered:
            directory = tracked.project_dir
            if directory and directory not in seen:
                seen.append(directory)
            if len(seen) >= limit:
                break
        return seen

    def action_new_session(self) -> None:
        """n: open a plain shell on the right, already tracked as a new
        session — no upfront directory/label form. You `cd` (or use one of
        the numbered shortcuts to a recent project directory already
        seeded into the shell) and type `claude` yourself; the claude shim
        already on this shell's $PATH (see _write_claude_shim/_shim_env —
        the same mechanism the rest of cagents' terminal features use)
        intercepts that into a real managed spawn, and _handle_spawn_request
        reuses this exact session id since it's still pending — no
        directory/mtime guessing needed to find the resulting transcript
        again after the fact."""
        directory = os.environ.get("CAGENTS_LAUNCH_CWD") or os.getcwd()
        session_id = str(uuid.uuid4())
        try:
            name = self.tmux.new_shell_session(
                directory, session_id=session_id, extra_env=self._shim_env()
            )
        except Exception as error:
            self.notify(f"Could not open a terminal: {error}", severity="error", timeout=10)
            return
        self._pending_new_terminals.add(session_id)
        try:
            self.tmux.send_shell_command(
                name, _new_terminal_seed_command(directory, self._recent_directories())
            )
        except Exception:
            pass  # cosmetic only — the terminal is still perfectly usable without it
        self._checkpoint("new session")
        self.store.track(session_id, directory, utcnow().isoformat())
        self.selected_session_id = session_id
        self._pending_highlight = session_id
        self._show_new_session(name)
        self.refresh_data()

    def action_track_session(self) -> None:
        self._load_track_candidates()

    @work(thread=True, exclusive=True, group="track", exit_on_error=False)
    def _load_track_candidates(self) -> None:
        from .claude_data import parse_session_file

        candidates = []
        for discovered in self.registry.discover_untracked()[:200]:
            title = discovered.session_id[:8]
            cwd = ""
            try:
                parsed = parse_session_file(
                    discovered.path, head_bytes=16 * 1024, tail_bytes=32 * 1024, preview_items=1
                )
                title = parsed.title
                cwd = parsed.cwd
            except OSError:
                pass
            candidates.append((discovered, title, cwd))
        self.call_from_thread(self._show_track_modal, candidates)

    def _show_track_modal(self, candidates: list) -> None:
        if not candidates:
            self.notify("No untracked sessions found in Claude's store.")
            return
        self._track_cwds = {d.session_id: cwd for d, _t, cwd in candidates}
        self.push_screen(TrackModal([(d, t) for d, t, _cwd in candidates]), self._track_chosen)

    def _track_chosen(self, session_id: str | None) -> None:
        if not session_id:
            return
        cwd = getattr(self, "_track_cwds", {}).get(session_id, "")
        self._checkpoint("track")
        self.store.track(session_id, cwd or str(Path.home()), utcnow().isoformat())
        self.selected_session_id = session_id
        self._pending_highlight = session_id
        self.refresh_data()
        self._notify_undoable("Session tracked.")

    def action_search(self) -> None:
        if not self.store.get_setting("conversation_search"):
            self.notify(
                "Conversation search is off — enable it in Settings (,).",
                severity="warning",
            )
            return
        self.push_screen(SearchModal(self.claude_dir), self._search_chosen)

    def _search_chosen(self, result) -> None:
        if result is None:
            return
        # Found via a full-history scan — it may not be a session cagents
        # is tracking yet; track() is a no-op if it already is.
        self._checkpoint("track")
        self.store.track(result.session_id, result.project_dir or str(Path.home()), utcnow().isoformat())
        self.selected_session_id = result.session_id
        self._pending_highlight = result.session_id
        self.refresh_data()

    # -- note / label / untrack --------------------------------------------------

    def action_rename(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        self.push_screen(
            InputModal("Rename", initial=view.tracked.label,
                       placeholder="display name (empty restores the AI title)"),
            lambda label: self._label_saved(view.session_id, label),
        )

    def _label_saved(self, session_id: str, label: str | None) -> None:
        if label is None:
            return
        self._checkpoint("rename")
        self.store.set_label(session_id, label.strip())
        self.refresh_data()
        self._notify_undoable("Renamed.")

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
        self._checkpoint("untrack")
        self.store.untrack(session_id)
        if self.selected_session_id == session_id:
            self.selected_session_id = None
        self.refresh_data()
        self._notify_undoable("Untracked.")

    # -- links / palette / settings ------------------------------------------------

    def action_open_link(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        url, label = "", "link"
        if view.parsed and view.parsed.links:
            link = view.parsed.links[-1]
            url, label = link.url, link.label
        elif view.tracked.pr_url:
            url, label = view.tracked.pr_url, "PR"
        if not url:
            self.push_screen(
                InputModal(
                    "No PR recorded for this session — paste one to associate",
                    placeholder="https://github.com/owner/repo/pull/123",
                ),
                lambda text: self._pr_associated(view.session_id, text, open_after=True),
            )
            return
        self._open_url(url, label)

    def action_open_jira(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        if not view.jira_key:
            self.notify("No Jira card linked to this session yet.", severity="warning")
            return
        self._open_url(view.jira_url, f"Jira {view.jira_key}")

    def _open_url(self, url: str, label: str) -> None:
        import subprocess

        try:
            subprocess.run(["open", url], check=True, timeout=10)
            self.notify(f"Opened {label}.")
        except Exception as error:
            self.notify(f"Could not open {label}: {error}", severity="error")

    def _pr_associated(self, session_id: str, text: str | None, open_after: bool = False) -> None:
        if not text or not text.strip():
            return
        url = text.strip()
        self._checkpoint("PR association")
        self.store.set_pr_url(session_id, url)
        if open_after:
            self._open_url(url, "PR")
        self.refresh_data()

    def action_palette(self) -> None:
        self.push_screen(PaletteModal(), self._palette_submitted)

    def _palette_submitted(self, request: str | None) -> None:
        if not request:
            return
        self.notify("Asking the fleet assistant… (plan will need your confirmation)")
        self._run_palette_request(request)

    @work(thread=True, exclusive=True, group="palette", exit_on_error=False)
    def _run_palette_request(self, request: str) -> None:
        snapshot = self.snapshot
        runner = self.claude_runner or CliClaudeRunner(claude_bin=self._claude_bin())
        try:
            raw = runner.run(build_prompt(snapshot, request))
            plan = parse_plan(raw, snapshot)
        except Exception as error:
            self.call_from_thread(
                self.notify, f"Fleet assistant failed: {error}", severity="error", timeout=10
            )
            return
        titles = {v.session_id: v.title for v in snapshot.views}
        self.call_from_thread(
            self.push_screen, PlanConfirmModal(plan, titles),
            lambda yes: self._plan_confirmed(plan, yes),
        )

    def _plan_confirmed(self, plan, yes: bool) -> None:
        if not yes:
            return
        self._checkpoint("fleet plan")
        done = apply_plan(plan, self.store, utcnow().isoformat())
        if done:
            self._notify_undoable("Applied: " + ", ".join(done))
        else:
            self.notify("Nothing to apply.")
        self.refresh_data()

    def action_settings(self) -> None:
        self.push_screen(SettingsModal(self.store, self._setting_changed))

    def _setting_changed(self, key: str, value) -> None:
        if key == "state_order":
            self.refresh_data()  # ranks are computed per refresh
            return
        if key == "diff_mode":
            self._write_context()  # the ctx keys read the mode from context
            return
        if key == "capture_left" and os.environ.get("CAGENTS_SIDECAR") == "1":
            try:
                apply_left_capture(bool(value))
            except Exception as error:
                self.notify(f"Could not apply ← binding: {error}", severity="error")
            return
        if key == "dim_chat_preview" and os.environ.get("CAGENTS_SIDECAR") == "1":
            try:
                apply_dim_chat(bool(value))
            except Exception as error:
                self.notify(f"Could not apply chat dimming: {error}", severity="error")
            return
        if key == "jira_integration" and value:
            if not jira.credentials_configured():
                self.notify(
                    "Jira columns are on, but no Jira credentials are set. Export "
                    "JIRA_SITE, JIRA_EMAIL, and JIRA_API_TOKEN in your shell profile "
                    "and restart cagents.",
                    title="Jira not configured",
                    severity="warning",
                    timeout=10,
                )
                return
            self.notify("Looking up linked Jira cards…")
            self._poll_jira_prs()

    # -- misc -------------------------------------------------------------------

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpModal())
