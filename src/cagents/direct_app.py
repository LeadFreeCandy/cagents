"""Use the existing supervisor UI with the native pane backend."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import sys

from .app import CagentsApp
from .direct import DirectSidecar, DirectTmux, direct_socket
from .sidecar import arrow_capture_commands
from .sockets import socket_name


class DirectApp(CagentsApp):
    def __init__(self, *args, tmux=None, sidecar=None, **kwargs):
        own = direct_socket()
        # Existing hosts are discovered, never moved. A suffix means QA/test
        # isolation, including discovery; it cannot attach a user's server.
        tmux = tmux or DirectTmux(sockets=(socket_name("claude"), socket_name("cagents-sessions"), own))
        if sidecar is None and DirectSidecar.enabled():
            sidecar = DirectSidecar(client=tmux)
        super().__init__(*args, tmux=tmux, sidecar=sidecar, **kwargs)

    @staticmethod
    def _ctx_prog():
        sibling = Path(sys.executable).parent / "cagents3-ctx"
        return str(sibling) if sibling.exists() else (shutil.which("cagents3-ctx") or "cagents3-ctx")

    def _configure_container(self):
        # DirectSidecar installs the single server's options and ctx bindings.
        pass

    def _workspace_ready(self):
        if self.sidecar is not None:
            self._apply_arrow_settings()
            self._apply_dim_chat(bool(self.store.get_setting("dim_chat_preview")))

    def _apply_arrow_settings(self):
        if self.sidecar is None:
            return
        aware = bool(self.store.get_setting("composer_aware_arrows"))
        for command in arrow_capture_commands(
            bool(self.store.get_setting("capture_left")),
            bool(self.store.get_setting("capture_ctrl_arrows")),
            probe=self._write_composer_probe() if aware else "",
        ):
            # Only an explicit size key changes geometry. Mouse focus, Enter,
            # queue navigation, and Ctrl-G leave the native viewport alone.
            command = [part.replace("select-pane -t :.0", "resize-pane -t :.0 -x 50% ; select-pane -t :.0")
                       .replace("select-pane -t :.1", "resize-pane -t :.0 -x 34 ; select-pane -t :.1")
                       for part in command]
            self.sidecar._run(command)

    def action_grow_session(self):
        if self.sidecar is not None:
            self.sidecar._run(["resize-pane", "-t", self.sidecar.own_pane, "-x", "34"])
        super().action_grow_session()

    def _apply_dim_chat(self, enable):
        if self.sidecar is None:
            return
        # Preserve the setting's visual cue without its old resize side effect.
        command = ("if -F '#{==:#{pane_index},0}' "
                   "'set -p -t :.1 window-style bg=colour234' "
                   "'set -pu -t :.1 window-style'") if enable else "set -pu -t :.1 window-style"
        # Moving a conversation also changes the active pane in its hidden
        # home window, which has no second pane. Only visible UI tabs dim chat.
        command = "if -F '#{&&:#{@cagents_tab},#{>:#{window_panes},1}}' " + shlex.quote(command)
        self.sidecar._run(["set-hook", "-g", "window-pane-changed", command])
        self.sidecar._run(["set", "-pu", "-t", ":.1", "window-style"])

    def _teardown_container(self):
        if self.sidecar is not None:
            self.sidecar.detach()

    def _can_auto_suspend(self, view):
        return view.tmux_socket == self.tmux.create_socket

    def _fullscreen_attach(self, name, socket):
        if self.sidecar is None:
            return super()._fullscreen_attach(name, socket)
        self.sidecar.show_viewer(self._attach_command(socket or self.tmux.create_socket, name))
        self.sidecar.focus_session()
        self.sidecar.hide_rail()
