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
from .claude_data import default_claude_dir, utcnow
from .ctx import CONTEXT_FILE, write_context
from .diffview import DiffResult, DiffScreen
from .format import header_summary, preview_renderable
from .handoff import first_message, summary_prompt
from .modals import (
    ConfirmModal,
    HelpModal,
    InputModal,
    NewSessionModal,
    PaletteModal,
    PlanConfirmModal,
    RelatedModal,
    SettingsModal,
    TrackModal,
)
from .notifier import notify_desktop, read_select_request
from .palette import CliClaudeRunner, apply_plan, build_prompt, parse_plan
from .sessions import SessionRegistry, SessionState, SessionView, Snapshot
from .sidecar import (
    Sidecar,
    apply_ctx_binds,
    apply_left_capture,
    nested_attach_command,
    preview_command,
)
from .store import Store
from .tmuxctl import TmuxClient
from .views import GroupedView, KanbanView, QueueView, SelectionChanged

REFRESH_SECONDS = 2.0
VIEWER_DEBOUNCE = 0.25  # settle time before the viewer follows the highlight
PR_POLL_SECONDS = 300.0
COMPACT_WIDTH = 60  # below this, the UI is a rail: no preview, dense rows

VIEW_IDS = ["grouped", "queue", "kanban"]

ALERT_STATES = (SessionState.NEEDS_INPUT, SessionState.NEEDS_REVIEW)


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
    """

    # Footer shows these in order; the important ones come first.
    BINDINGS = [
        Binding("enter", "attach", "Attach"),
        Binding("d", "toggle_done", "Done"),
        Binding("w", "toggle_waiting", "Waiting"),
        Binding("f", "fork", "Fork"),
        Binding("h", "handoff", "Handoff"),
        Binding("D", "show_diff", "Diff"),
        Binding("n", "new_session", "New"),
        Binding("a", "track_session", "Track", show=False),
        Binding("1", "switch_view('grouped')", "Grouped", show=False),
        Binding("2", "switch_view('queue')", "Queue", show=False),
        Binding("3", "switch_view('kanban')", "Kanban", show=False),
        Binding("tab", "next_view", "Next view", show=False, priority=True),
        Binding("right", "grow_session", "Grow session", show=False),
        Binding("asterisk", "related", "Related", show=False),
        Binding("o", "open_link", "Open link", show=False),
        Binding("R", "rename", "Rename", show=False),
        Binding("x", "untrack", "Untrack", show=False),
        Binding("colon", "palette", "Fleet", show=False),
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
        self.gh_runner = gh_runner  # injectable for the PR poller
        self.snapshot = Snapshot()
        self.active_view_id = "grouped"
        self.selected_session_id: str | None = None
        self.compact = False
        self._prev_states: dict[str, SessionState] = {}
        self._seen_first_snapshot = False
        self._viewer_timer = None
        self._viewer_target: str = ""

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
        if self.sidecar is not None:
            # The right pane IS the preview; the internal one never shows.
            self.query_one("#body").add_class("sidecar")
        self._apply_compact(self.size.width)
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)
        self.set_interval(PR_POLL_SECONDS, self._poll_waiting_prs)
        self.query_one("#grouped", GroupedView).focus_list()
        if os.environ.get("CAGENTS_SIDECAR") == "1" and self.sidecar is not None:
            try:
                apply_left_capture(bool(self.store.get_setting("capture_left")))
                apply_ctx_binds(self._ctx_prog(), str(self._context_path()))
            except Exception as error:
                self.notify(f"Container key setup failed: {error}", severity="warning")
        if self.sidecar is not None:
            try:
                self.sidecar.ensure_workspace(
                    os.environ.get("CAGENTS_LAUNCH_CWD") or os.getcwd()
                )
            except Exception as error:
                self.notify(f"Workspace setup failed: {error}", severity="error")

    def notify(self, message, *, title="", severity="information", timeout=None, **kwargs):
        """Routine toasts are opt-in (settings). Warnings and errors always
        show — silent failure is the one unforgivable sin (spec §11)."""
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
        if self._viewer_timer is not None:
            self._viewer_timer.stop()
        self._viewer_timer = self.set_timer(VIEWER_DEBOUNCE, self._sync_viewer)

    def _viewer_command(self, view: SessionView) -> str:
        if view.live:
            socket = view.tmux_socket or self.tmux.create_socket
            return nested_attach_command(socket, view.tmux_name)
        return preview_command(view.session_id, str(self.store.path), str(self.claude_dir))

    def _sync_viewer(self) -> None:
        if self.sidecar is None:
            return
        view = self.selected_view()
        if view is None:
            return
        command = self._viewer_command(view)
        if command == self._viewer_target:
            return
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
            write_context(self._context_path(), view.work_dir, view.session_id)

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
        view = self.selected_view()
        if view is None:
            self.notify("No session selected.", severity="warning")
            return
        if not self.tmux.available():
            self.notify("tmux not found on PATH — cannot attach.", severity="error")
            return
        try:
            if view.live:
                self._attach_live(view)
            else:
                self._resume_dead_session(view)
        except Exception as error:  # loud, specific failure (spec §11)
            self.notify(f"Attach failed: {error}", severity="error", timeout=10)
        self.refresh_data()

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
        if view.state == SessionState.WORKING:
            # Actively writing its transcript but hosted somewhere cagents
            # can't see (cmux, a bare terminal). Resuming would put a second
            # live CLI on one conversation — refuse.
            self.notify(
                "This session is running outside cagents' tmux right now — "
                "attach from wherever it lives, or wait for it to finish.",
                severity="warning", timeout=8,
            )
            return
        if view.missing:
            self.notify(
                "This session's transcript is gone from Claude's store; nothing to resume.",
                severity="error", timeout=10,
            )
            return
        directory = view.work_dir if Path(view.work_dir).is_dir() else view.project_dir
        if not Path(directory).is_dir():
            self.notify(
                f"Project directory no longer exists: {directory}", severity="error", timeout=10
            )
            return
        claude_bin = self._claude_bin()
        if not claude_bin:
            self.notify("claude CLI not found.", severity="error")
            return
        name = self.tmux.new_claude_session(
            directory, ["--resume", view.session_id],
            session_id=view.session_id, claude_bin=claude_bin,
        )
        self._show_new_session(name)

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

    def _claude_bin(self) -> str:
        found = shutil.which("claude")
        if found:
            return found
        fallback = Path.home() / ".local" / "bin" / "claude"
        return str(fallback) if fallback.exists() else ""

    # -- done / waiting ---------------------------------------------------------

    def action_toggle_done(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        if view.state == SessionState.DONE:
            self.store.clear_reviewed(view.session_id)
            self.notify("Un-done — back in the queue.")
        elif view.state in (SessionState.WORKING, SessionState.NEEDS_INPUT):
            self.notify("Still in flight — mark it done when Claude is finished.", severity="warning")
            return
        else:
            self.store.mark_reviewed(view.session_id, utcnow().isoformat())
            self.notify("Done.")
        self.refresh_data()

    def action_toggle_waiting(self) -> None:
        view = self.selected_view()
        if view is None:
            return
        if view.state == SessionState.WAITING_EXTERNAL:
            self.store.clear_waiting(view.session_id)
            self.notify("No longer waiting on the PR.")
            self.refresh_data()
            return
        if view.state in (SessionState.WORKING, SessionState.NEEDS_INPUT):
            self.notify("Still in flight — park it once Claude is finished.", severity="warning")
            return
        # Prefer a PR the session itself recorded; else look one up; else ask.
        recorded = ""
        if view.parsed:
            for link in reversed(view.parsed.links):
                if link.kind == "pr" and link.url:
                    recorded = link.url
                    break
        if recorded:
            self._set_waiting(view.session_id, recorded)
        else:
            self._find_pr_worker(view.session_id, view.project_dir)

    @work(thread=True, exclusive=True, group="findpr")
    def _find_pr_worker(self, session_id: str, directory: str) -> None:
        url = gitops.find_pr_url(directory, runner=self.gh_runner)
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
        self._set_waiting(session_id, text.strip())

    def _set_waiting(self, session_id: str, pr_url: str) -> None:
        self.store.set_waiting(session_id, utcnow().isoformat(), pr_url)
        self.notify(f"Waiting on {pr_url} — comments re-alert; merge marks it done.")
        self.refresh_data()

    def _poll_waiting_prs(self) -> None:
        self._poll_waiting_worker()

    @work(thread=True, exclusive=True, group="prpoll")
    def _poll_waiting_worker(self) -> None:
        changed = False
        for tracked in list(self.store.sessions.values()):
            if not tracked.waiting_since or not tracked.waiting_pr:
                continue
            try:
                status = gitops.pr_status(tracked.waiting_pr, runner=self.gh_runner)
            except Exception:
                continue  # transient gh failure; try again next poll
            if status.merged:
                self.store.mark_reviewed(tracked.session_id, utcnow().isoformat())
                self.store.clear_waiting(tracked.session_id, external_update="merged")
                changed = True
                self._desktop_note(
                    f"PR merged — {tracked.label or tracked.session_id[:8]} is done",
                    tracked.session_id,
                )
            elif status.last_activity and status.last_activity > tracked.waiting_since:
                self.store.clear_waiting(tracked.session_id, external_update="github comments")
                changed = True
                self._desktop_note(
                    f"New PR activity — {tracked.label or tracked.session_id[:8]} needs review",
                    tracked.session_id,
                )
        if changed:
            self.call_from_thread(self.refresh_data)

    def _desktop_note(self, message: str, session_id: str) -> None:
        if self.store.get_setting("desktop_notifications"):
            notify_desktop("cagents", message, session_id, self.store.path.parent)

    # -- desktop notifications / click-select ------------------------------------

    def _notify_transitions(self, snapshot: Snapshot) -> None:
        first = not self._seen_first_snapshot
        self._seen_first_snapshot = True
        enabled = bool(self.store.get_setting("desktop_notifications"))
        for view in snapshot.views:
            previous = self._prev_states.get(view.session_id)
            self._prev_states[view.session_id] = view.state
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
                    thread=True,
                )

    def _handle_select_request(self) -> None:
        session_id = read_select_request(self.store.path.parent)
        if not session_id or self.snapshot.by_id(session_id) is None:
            return
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
            name = self.tmux.new_claude_session(
                view.project_dir,
                ["--resume", source_id, "--fork-session", "--session-id", new_id],
                session_id=new_id, claude_bin=claude_bin,
            )
        except Exception as error:
            self.notify(f"Fork failed: {error}", severity="error", timeout=10)
            return
        self.store.track(
            new_id, view.project_dir, utcnow().isoformat(), label=prompt[:60],
            parent_id=source_id, relation="fork",
        )
        self.selected_session_id = new_id
        self._show_new_session(name)
        self._send_prompt_later(name, prompt, "Forked — prompt sent to the new session.")
        self.refresh_data()

    @work(thread=True, group="send")
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
        new_id = str(uuid.uuid4())
        try:
            name = self.tmux.new_claude_session(
                view.project_dir, ["--session-id", new_id],
                session_id=new_id, claude_bin=self._claude_bin(),
            )
        except Exception as error:
            self.notify(f"Handoff session failed to start: {error}", severity="error", timeout=10)
            return
        self.store.track(
            new_id, view.project_dir, utcnow().isoformat(), label=prompt[:60],
            parent_id=source_id, relation="handoff",
        )
        # The predecessor is done — restore anytime with d.
        self.store.mark_reviewed(source_id, utcnow().isoformat())
        self.selected_session_id = new_id
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

    @work(thread=True, exclusive=True, group="sendreview")
    def _send_review_worker(self, view: SessionView, message: str, count: int) -> None:
        import time

        try:
            if view.live:
                tmux_name, socket = view.tmux_name, (view.tmux_socket or None)
            else:
                tmux_name = self.tmux.new_claude_session(
                    view.project_dir, ["--resume", view.session_id],
                    session_id=view.session_id, claude_bin=self._claude_bin(),
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

    def action_new_session(self) -> None:
        initial = os.environ.get("CAGENTS_LAUNCH_CWD") or os.getcwd()
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
                directory, ["--session-id", session_id],
                session_id=session_id, claude_bin=claude_bin,
            )
        except Exception as error:
            self.notify(f"Could not start session: {error}", severity="error", timeout=10)
            return
        self.store.track(session_id, directory, utcnow().isoformat(), label=label)
        self.selected_session_id = session_id
        self._show_new_session(name)
        self.refresh_data()

    def action_track_session(self) -> None:
        self._load_track_candidates()

    @work(thread=True, exclusive=True, group="track")
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
        self.store.track(session_id, cwd or str(Path.home()), utcnow().isoformat())
        self.selected_session_id = session_id
        self.refresh_data()
        self.notify("Session tracked.")

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

    # -- links / palette / settings ------------------------------------------------

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
        done = apply_plan(plan, self.store, utcnow().isoformat())
        self.notify("Applied: " + ", ".join(done) if done else "Nothing to apply.")
        self.refresh_data()

    def action_settings(self) -> None:
        self.push_screen(SettingsModal(self.store, self._setting_changed))

    def _setting_changed(self, key: str, value) -> None:
        if key == "state_order":
            self.refresh_data()  # ranks are computed per refresh
            return
        if key == "capture_left" and os.environ.get("CAGENTS_SIDECAR") == "1":
            try:
                apply_left_capture(bool(value))
            except Exception as error:
                self.notify(f"Could not apply ← binding: {error}", severity="error")

    # -- misc -------------------------------------------------------------------

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_help(self) -> None:
        self.push_screen(HelpModal())
