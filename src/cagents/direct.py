"""Native tmux panes for cagents3.

The dashboard never reads or forwards terminal bytes. Each conversation owns a
pane and a hidden home window. Swapping that pane into a tab preserves the PTY,
process, scrollback, selection and draft. Pane tags, not window positions, are
the source of truth for discovery and lifecycle operations.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid

from .sidecar import Sidecar, nested_attach_command, ctx_bind_commands
from .sockets import socket_name
from .tmuxctl import TmuxClient, TmuxSession, terminal_input_commands

SESSION = "cagents3"


def direct_socket() -> str:
    return os.environ.get("CAGENTS_DIRECT_SOCKET") or socket_name("cagents3")


_thread_lock = threading.RLock()
_local = threading.local()


@contextmanager
def server_lock(socket: str):
    """Serialize pane moves from dashboard workers and native tab hooks.

    The lock is reentrant within a thread. The file lock covers separate ctx
    processes; it never encloses an agent shutdown or a network operation.
    """
    with _thread_lock:
        held = getattr(_local, "held", set())
        if socket in held:
            yield
            return
        key = hashlib.sha256(socket.encode()).hexdigest()[:24]
        path = Path(tempfile.gettempdir()) / f"cagents3-{os.getuid()}-{key}.lock"
        with path.open("a") as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The layout is busy; please retry.")
                    time.sleep(.01)
            _local.held = held | {socket}
            try:
                yield
            finally:
                _local.held = held
                fcntl.flock(lock, fcntl.LOCK_UN)


def _placeholder(message: str) -> str:
    # remain-on-exit keeps the terminal cells. No sleeping process per slot.
    return "printf '%s\\n' " + shlex.quote(message)


class DirectTmux(TmuxClient):
    def __init__(self, sockets=None, create_socket=None, tmux_bin="tmux"):
        own = create_socket or direct_socket()
        super().__init__(sockets=sockets or (own,), create_socket=own, tmux_bin=tmux_bin)

    def call(self, *args: str) -> str:
        proc = self._run(self.create_socket, *args)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "tmux command failed")
        return proc.stdout.strip()

    def pane(self, name: str) -> str:
        rows = self.call("list-panes", "-a", "-F", "#{pane_id}\t#{@cagents_name}")
        for line in rows.splitlines():
            pane, _, label = line.partition("\t")
            if (name.startswith("%") and pane == name) or label == name:
                return pane
        raise RuntimeError("Conversation pane is no longer available; refresh and retry.")

    def _pane_target(self, name: str, socket: str) -> str:
        return self.pane(name) if socket == self.create_socket else super()._pane_target(name, socket)

    def _list_on(self, socket: str) -> list[TmuxSession]:
        if socket != self.create_socket:
            return super()._list_on(socket)
        fields = ("@cagents_role", "@cagents_name", "@cagents_session_id", "@cagents_created",
                  "pane_pid", "pane_current_path", "pane_current_command", "pane_id", "pane_dead",
                  "@cagents_suspended", "session_attached", "window_active", "window_activity")
        result = self._run(socket, "list-panes", "-a", "-F", "\x1f".join("#{" + f + "}" for f in fields))
        rows = []
        for line in result.stdout.splitlines() if result.returncode == 0 else ():
            parts = line.split("\x1f")
            if len(parts) != len(fields) or parts[0] != "agent":
                continue
            _, name, sid, created, pid, cwd, command, pane, dead, suspended, attached, active, activity = parts
            try:
                rows.append(TmuxSession(name, float(created), float(activity or created),
                                        attached != "0" and active == "1", int(pid), cwd, socket,
                                        cagents_session_id=sid, pane_command=command, pane_id=pane,
                                        pane_dead=dead == "1", suspended=suspended == "1"))
            except ValueError:
                continue
        return rows

    def _prepare_agent_command(self, command, session_id, socket):
        return command if socket == self.create_socket else super()._prepare_agent_command(command, session_id, socket)

    def _respawn_environment(self, pane_id, socket):
        if socket != self.create_socket:
            return super()._respawn_environment(pane_id, socket)
        import json
        value = self.call("display-message", "-p", "-t", pane_id, "#{@cagents_environment}")
        environment = json.loads(value or "[]")
        if not isinstance(environment, list) or len(environment) % 2 or any(
            environment[i] != "-e" or not isinstance(environment[i+1], str)
            for i in range(0, len(environment), 2)
        ):
            raise RuntimeError("Cannot verify this pane's original environment.")
        return environment

    def get_session_env(self, session_name, var, socket=None):
        socket = socket or self.create_socket
        if socket != self.create_socket:
            return super().get_session_env(session_name, var, socket)
        if var == "CAGENTS_SESSION_ID":
            return self.call("display-message", "-p", "-t", self.pane(session_name), "#{@cagents_session_id}")
        return ""

    def _new_home(self, name, directory, command="", sid="", role="agent", env=None, owner=""):
        import json
        with server_lock(self.create_socket):
            if command:
                launch = [command]
            else:
                # respawn-pane with no command repeats the startup placeholder.
                # Match a fresh tmux window: default-command, or a login shell.
                default = self.call("show-option", "-Av", "-t", SESSION, "default-command")
                launch = [default] if default else [
                    self.call("show-option", "-Av", "-t", SESSION, "default-shell"), "-l"
                ]
            environment = list(env or ())
            if sid:
                environment += ["-e", f"CAGENTS_SESSION_ID={sid}"]
            args = ["new-window", "-d", "-P", "-F", "#{pane_id}\t#{window_id}",
                    "-t", f"={SESSION}:", "-n", name, "-c", directory, *environment]
            # Create an exited slot first, so a fast-exiting CLI cannot destroy
            # its window before remain-on-exit and identity tags are installed.
            pane, home = self.call(*args, _placeholder("starting…")).split("\t")
            for option in ("window-status-format", "window-status-current-format"):
                self.call("set", "-w", "-t", home, option, "")
            self.call("set", "-w", "-t", home, "window-size", "manual")
            # Start at the actual content width, avoiding a full-width first
            # paint followed by an immediate resize when the pane is shown.
            size = self.call("display-message", "-p", "-t", f"={SESSION}:session.1",
                             "#{pane_width}\t#{pane_height}").split("\t")
            self.call("resize-window", "-t", home, "-x", size[0], "-y", size[1])
            self.call("set", "-p", "-t", pane, "remain-on-exit", "on")
            tags = {"name": name, "session_id": sid, "owner": owner,
                    "created": str(time.time()), "home": home, "environment": json.dumps(environment)}
            for key, value in tags.items():
                self.call("set", "-p", "-t", pane, "@cagents_" + key, value)
            self.call("respawn-pane", "-k", "-t", pane, *environment, *launch)
            # Publish to the registry only after the final process owns the
            # pane. Otherwise a concurrent refresh observes the startup slot's
            # PID as the agent and immediately has a stale lifecycle target.
            self.call("set", "-p", "-t", pane, "@cagents_role", role)
            return name

    def _unique_name(self, base):
        base = "agent-" + (re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-") or "session")
        existing = set(self.call("list-windows", "-t", f"={SESSION}", "-F", "#W").splitlines())
        name, number = base, 2
        while name in existing:
            name, number = f"{base}-{number}", number + 1
        return name

    def new_claude_session(self, directory, claude_args, session_id="", claude_bin="", extra_env=None):
        import shutil
        name = self._unique_name(Path(directory).name)
        binary = claude_bin or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        # This window belongs to the attached session, so native OSC color
        # queries go straight to tmux's terminal client. No color helper.
        return self._new_home(name, directory, shlex.join([binary, *claude_args]),
                              session_id, env=extra_env)

    def new_shell_session(self, directory, session_id="", extra_env=None):
        return self._new_home(self._unique_name(Path(directory).name), directory,
                              sid=session_id, env=extra_env)

    def ensure_session_window(self, session_name, window_name, directory, socket=None):
        socket = socket or self.create_socket
        if socket != self.create_socket:
            return super().ensure_session_window(session_name, window_name, directory, socket)
        name = f"{session_name}--{window_name}"
        with server_lock(socket):
            try:
                self.pane(name)
                return
            except RuntimeError:
                pass
            sid = self.get_session_env(session_name, "CAGENTS_SESSION_ID", socket)
            # Match the dashboard's shim environment for native shell tabs.
            import json
            raw = self.call("show-option", "-gqv", "@cagents_term_env")
            self._new_home(name, directory, sid=sid, role="terminal", env=json.loads(raw or "[]"), owner=session_name)

    def ensure_window_view(self, session_name, window_name, socket=None, force_select=False):
        socket = socket or self.create_socket
        if socket != self.create_socket:
            return super().ensure_window_view(session_name, window_name, socket, force_select)
        return self.pane(f"{session_name}--{window_name}")

    def has_session(self, session_name, socket=None):
        if (socket or self.create_socket) != self.create_socket:
            return super().has_session(session_name, socket)
        try:
            self.pane(session_name)
            return True
        except RuntimeError:
            return False

    def client_activity(self):
        # An input timestamp belongs to the focused conversation, not every
        # agent in this shared tmux session. Rail activity is tracked by the UI.
        result = {}
        fmt = "#{@cagents_name}\t#{@cagents_role}\t#{client_activity}\t#{@cagents_owner}"
        p = self._run(self.create_socket, "list-clients", "-F", fmt)
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 4 and parts[1] in ("agent", "terminal"):
                name = parts[3] if parts[1] == "terminal" else parts[0]
                if name:
                    result[f"{self.create_socket}:{name}"] = float(parts[2])
        for socket in self.sockets:
            if socket != self.create_socket:
                result.update(TmuxClient(sockets=(socket,)).client_activity())
        return result

    def session_statusline_on(self, session_name, socket=None):
        if (socket or self.create_socket) != self.create_socket:
            super().session_statusline_on(session_name, socket)

    def session_statusline_off(self, session_name, socket=None):
        if (socket or self.create_socket) != self.create_socket:
            super().session_statusline_off(session_name, socket)


def setup_commands():
    """One set of terminal options, with no focus-driven geometry changes."""
    return [
        ["set", "-g", "mouse", "on"], ["set", "-g", "escape-time", "10"],
        ["set", "-g", "focus-events", "on"],
        ["set", "-g", "status", "on"], ["set", "-g", "status-position", "top"],
        ["set", "-g", "status-style", "bg=colour236,fg=colour248"],
        ["set", "-g", "status-left", " cagents3 "],
        ["set", "-g", "status-right", " C-g queue · C-t term · C-d diff "],
        ["set", "-g", "status-right-length", "42"],
        ["set", "-g", "window-status-format", "  #W  "],
        ["set", "-g", "window-status-current-format", "#[bg=colour31,fg=colour231,bold]  #W  #[default]"],
        ["set", "-g", "window-status-separator", ""],
        ["set", "-gw", "remain-on-exit", "on"],
        ["set", "-gw", "remain-on-exit-format", ""],
        ["set", "-gw", "automatic-rename", "off"],
        ["set", "-g", "allow-rename", "off"],
        ["unbind", "-n", "WheelUpStatus"], ["unbind", "-n", "WheelDownStatus"],
        ["set-hook", "-gu", "after-select-pane"],
        ["set-hook", "-gu", "window-pane-changed"],
        *terminal_input_commands(),
    ]


class DirectSidecar:
    enabled = staticmethod(Sidecar.enabled)

    def __init__(self, runner=None, own_pane="", client=None):
        self.client = client or DirectTmux()
        self.socket = self.client.create_socket
        self._run = runner or (lambda args: self.client.call(*args))
        self.own_pane = own_pane or os.environ.get("TMUX_PANE", "")
        self.term_env = []

    def _option(self, target, key):
        return self._run(["display-message", "-p", "-t", target, "#{" + key + "}"])

    @property
    def pane_id(self):
        return self._option(f"={SESSION}:session.1", "pane_id")

    def attach_command(self, socket, name):
        if socket == self.socket:
            return self.client.pane(name)
        return nested_attach_command(socket, name)

    def _ensure_tab(self, name, cwd="", command=""):
        windows = self._run(["list-windows", "-t", f"={SESSION}", "-F", "#W"]).splitlines()
        if name not in windows:
            target = ["-b", "-t", f"={SESSION}:new-term"] if "new-term" in windows else ["-t", f"={SESSION}:"]
            self._run(["new-window", "-d", *target, "-n", name, _placeholder("")])
        target = f"={SESSION}:{name}"
        if self._option(target, "window_panes") == "1":
            args = ["split-window", "-h", "-d", "-t", target]
            if cwd:
                args += ["-c", cwd, *self.term_env]
            self._run([*args, command or _placeholder("Select a conversation")])
            width = self._option(self.own_pane, "pane_width") if name != "session" else "50%"
            self._run(["resize-pane", "-t", target + ".0", "-x", width])
        self._run(["set", "-w", "-t", target, "@cagents_tab", "1"])

    def ensure_workspace(self, terminal_dir="", ctx_prog="", context_path="", shim_env=None):
        import json
        with server_lock(self.socket):
            self.term_env = list(shim_env or ())
            for cmd in setup_commands():
                self._run(cmd)
            self._run(["set", "-g", "@cagents_rail", self.own_pane])
            self._run(["set", "-g", "@cagents_term_env", json.dumps(self.term_env)])
            for name in ("session", "diff", "term-1"):
                self._ensure_tab(name, cwd=terminal_dir if name == "term-1" else "",
                                 command=os.environ.get("SHELL", "/bin/zsh") if name == "term-1" else "")
            # Pure tmux: the existing Textual process moves to the left slot of
            # the newly selected tab. Content and queue retain their pane IDs.
            hook = f"if -F '#{{@cagents_tab}}' 'swap-pane -d -s {self.own_pane} -t :.0'"
            self._run(["set-hook", "-gu", "after-select-window"])
            self._run(["set-hook", "-g", "after-select-window[0]", hook])
            if ctx_prog and context_path:
                for cmd in ctx_bind_commands(ctx_prog, context_path):
                    self._run(cmd)
                prog = shlex.join([ctx_prog, "tab", "--context", context_path])
                self._run(["set-hook", "-g", "after-select-window[1]", f"run-shell -b {shlex.quote(prog)}"])
                # A native window labelled +term is the same entry point as
                # before, but its shell will be another direct pane.
                self._ensure_tab("new-term")
                for option in ("window-status-format", "window-status-current-format"):
                    self._run(["set", "-w", "-t", f"={SESSION}:new-term", option, "  +term  "])

    def _park(self, pane):
        home = self._option(pane, "@cagents_home")
        if not home:
            return
        if self._option(pane, "window_id") == home:
            return
        width = self._option(pane, "pane_width")
        height = self._option(pane, "pane_height")
        self._run(["resize-window", "-t", home, "-x", width, "-y", height])
        self._run(["swap-pane", "-dZ", "-s", pane, "-t", home + ".0"])

    def _show(self, tab, command, stale=lambda: False):
        with server_lock(self.socket):
            if stale():
                return
            slot = f"={SESSION}:{tab}.1"
            current = self._option(slot, "pane_id")
            native = bool(re.fullmatch(r"%\d+", command))
            if native and current == command:
                return
            if not native and self._option(current, "@cagents_command") == command and self._option(current, "pane_dead") != "1":
                return
            if native:
                # Resolve before parking the previous view. A disappeared
                # target must never blank an otherwise healthy conversation.
                self.client.pane(command)
            if stale():
                return
            self._park(current)
            current = self._option(slot, "pane_id")
            if native:
                self._run(["swap-pane", "-dZ", "-s", command, "-t", current])
            else:
                self._run(["respawn-pane", "-k", "-t", current, command])
                self._run(["set", "-p", "-t", current, "@cagents_command", command])

    def show_viewer(self, shell_command, stale=lambda: False):
        self._show("session", shell_command, stale)

    def sync_terminal_tab(self, shell_command, stale=lambda: False):
        self._show("term-1", shell_command, stale)

    def open_terminal_tab(self, shell_command):
        self.sync_terminal_tab(shell_command)
        self.select_tab("term-1")

    def open_diff_tab(self, pager_command):
        self._show("diff", pager_command)
        self.select_tab("diff")

    def select_tab(self, name):
        self._run(["select-window", "-t", f"={SESSION}:{name}"])

    def focus_session(self):
        self.select_tab("session")
        self._run(["select-pane", "-t", self.pane_id])

    def focus_pane(self):
        self._run(["select-pane", "-t", ":.1"])

    def focus_rail(self):
        self._run(["select-pane", "-t", self.own_pane])

    def hide_rail(self):
        self.focus_pane()
        if self._option(":.1", "window_zoomed_flag") != "1":
            self._run(["resize-pane", "-Z", "-t", ":.1"])

    def detach(self):
        # Agents share this server. Quitting a dashboard must never kill it.
        self._run(["detach-client", "-s", SESSION])
