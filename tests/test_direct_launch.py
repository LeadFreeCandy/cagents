"""Launch the real Textual dashboard in a PTY and drive its public keys."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_real_dashboard_new_conversation_quit_and_relaunch_preserve_panes(tmp_path):
    socket = "cg-launch-" + uuid.uuid4().hex[:12]
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"version": 1, "sessions": {}, "settings": {"desktop_notifications": False}}))
    env = {**os.environ, "CAGENTS_DIRECT_SOCKET": socket,
           "CAGENTS_SOCKET_SUFFIX": "-" + socket, "TERM": "xterm-256color", "COLORTERM": "truecolor",
           "CAGENTS_LAUNCH_CWD": str(tmp_path)}
    for key in ("TMUX", "TMUX_PANE", "CAGENTS_SIDECAR", "CAGENTS_DIRECT", "NO_COLOR"):
        env.pop(key, None)
    clients = []

    def tmux(*args):
        p = subprocess.run(["tmux", "-L", socket, *args], env=env,
                           text=True, capture_output=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else ""

    def launch():
        pid, master = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 140, 0, 0))
            os.execvpe(sys.executable, [sys.executable, "-m", "cagents.direct_cli", "--store", str(state),
                                      "--claude-dir", str(tmp_path / "claude"),
                                      "--codex-dir", str(tmp_path / "codex")], env)
        clients.append((pid, master))
        return master

    def pump(master, duration=.15):
        end = time.monotonic() + duration
        output = b""
        while time.monotonic() < end:
            if select.select([master], [], [], .02)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output += data
                for match in re.finditer(rb"\x1b\](10|11);\?(?:\x07|\x1b\\)", data):
                    value = b"2121/2525/2b2b" if match[1] == b"11" else b"dddd/dddd/dddd"
                    os.write(master, b"\x1b]" + match[1] + b";rgb:" + value + b"\x1b\\")
        return output

    def wait(master, condition, message):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            pump(master)
            if condition():
                return
        raise AssertionError(message + "\n" + tmux("capture-pane", "-p", "-t", "%0"))

    def agents():
        rows = tmux("list-panes", "-a", "-F", "#{@cagents_role}:#{pane_id}:#{pane_pid}")
        return sorted(row for row in rows.splitlines() if row.startswith("agent:"))

    try:
        master = launch()
        wait(master, lambda: "new-term" in tmux("list-windows", "-F", "#W"), "dashboard failed to create native tabs")
        pump(master, .5)
        os.write(master, b"n")
        wait(master, lambda: len(agents()) == 1, "n did not open a native conversation terminal")
        first = agents()
        rail = tmux("show-option", "-gqv", "@cagents_rail")
        wait(master, lambda: tmux("display-message", "-p", "#{pane_id}") != rail, "new conversation did not receive focus")
        os.write(master, b"\x07")  # return from the real shell to the queue
        wait(master, lambda: tmux("display-message", "-p", "#{pane_id}") == rail, "Ctrl-G did not reach the queue")
        pump(master, .2)
        os.write(master, b"n")
        wait(master, lambda: len(agents()) == 2, "second conversation did not open")
        assert all(row in agents() for row in first)
        before = agents()
        opened = next(row.split(":")[1] for row in before if row not in first)
        # A process is registered before the UI has finished showing it.
        # Drive the next key from the visible conversation, as a user would.
        wait(master, lambda: tmux("display-message", "-p", "#{pane_id}") == opened, "second conversation did not receive focus")
        os.write(master, b"\x07")
        wait(master, lambda: tmux("display-message", "-p", "#{pane_id}") == rail, "Ctrl-G did not return to the queue")
        pump(master, .3)
        os.write(master, b"q")
        wait(master, lambda: tmux("display-message", "-p", "-t", rail, "#{pane_dead}") == "1", "q did not exit the dashboard")
        assert agents() == before
        master = launch()
        wait(master, lambda: tmux("display-message", "-p", "-t", rail, "#{pane_dead}") == "0", "relaunch did not restart dashboard")
        pump(master, 1)
        assert agents() == before
        assert "Traceback" not in tmux("capture-pane", "-p", "-t", rail)
        assert len(tmux("list-clients", "-F", "#{client_pid}").splitlines()) == 1
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], env=env, capture_output=True, timeout=5)
        for pid, master in clients:
            os.close(master)
            os.waitpid(pid, 0)
