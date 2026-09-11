"""Native scroll routing, including real wheel packets through nested tmux."""
import os
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from cagents.tmuxctl import terminal_input_commands
from cagents.sidecar import nested_attach_command

@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_wheel_packets_reach_mouse_app_once_without_changing_focus(tmp_path):
    """Same mouse protocol Claude uses; send genuine SGR wheel input to a PTY.

    The right outer pane is focused, but the wheel hovers the left viewer.
    All three servers must forward exactly once, retaining direction/position.
    """
    import fcntl
    import pty
    import struct
    import termios

    sockets = [f"cg-wheel-{uuid.uuid4().hex[:12]}-{i}" for i in range(3)]
    native, work, outer = sockets
    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("TMUX", None)
    recorder = tmp_path / "mouse_app.py"
    received = tmp_path / "events"
    recorder.write_text(
        "import os,sys,tty\n"
        "tty.setraw(0)\n"
        "os.write(1,b'\\x1b[?1000h\\x1b[?1006hready')\n"
        "with open(sys.argv[1], 'ab', buffering=0) as stream:\n"
        " while True:\n"
        "  stream.write(os.read(0,4096))\n"
    )
    def tmux(socket, *args):
        return subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args],
                              env=env, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    master = pid = None
    try:
        for socket in sockets:
            tmux(socket, "new-session", "-d", "-s", "test", "-x", "90", "-y", "18", "sleep 60")
            tmux(socket, "set", "-g", "mouse", "on")
            tmux(socket, "set", "-g", "status", "off")
        for command in terminal_input_commands():
            tmux(native, *command)
        for socket in (work, outer):
            for command in terminal_input_commands():
                tmux(socket, *command)
        tmux(native, "respawn-pane", "-k", shlex.join([sys.executable, str(recorder), str(received)]))
        tmux(work, "respawn-pane", "-k", nested_attach_command(native, "test"))
        tmux(outer, "respawn-pane", "-k", nested_attach_command(work, "test"))
        focused = tmux(outer, "split-window", "-h", "-l", "20", "-P", "-F", "#{pane_id}", "sleep 60")

        pid, master = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 90, 0, 0))
            os.execvpe("tmux", ["tmux", "-L", outer, "attach-session", "-t", "=test"], env)

        # Wait for mouse reporting to propagate before sending terminal input.
        deadline, output = time.monotonic() + 3, b""
        while time.monotonic() < deadline and b"?1006h" not in output:
            if select.select([master], [], [], .05)[0]:
                output += os.read(master, 65536)
        assert b"?1006h" in output
        for socket in sockets:
            assert tmux(socket, "display-message", "-p", "-t", "%0", "#{mouse_any_flag}") == "1"
        packets = b"\x1b[<64;10;8M" * 7 + b"\x1b[<65;10;8M" * 4
        os.write(master, packets)
        deadline = time.monotonic() + 3
        data = b""
        while time.monotonic() < deadline:
            if received.exists():
                data = received.read_bytes()
            if data == packets:
                break
            time.sleep(.01)
        assert data == packets
        assert tmux(outer, "display-message", "-p", "#{pane_id}") == focused
        for socket in sockets:
            assert tmux(socket, "display-message", "-p", "-t", "%0", "#{pane_in_mode}") == "0"
    finally:
        if master is not None:
            os.close(master)
        if pid:
            os.waitpid(pid, 0)
        for socket in reversed(sockets):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=5)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
@pytest.mark.parametrize("mode_keys", ["emacs", "vi"])
def test_terminal_history_scrolls_on_first_tick_and_returns_to_live_output(tmp_path, mode_keys):
    """Real wheel bursts through three tmux servers, with an untouched draft."""
    import fcntl
    import pty
    import struct
    import termios

    sockets = [f"cg-history-{uuid.uuid4().hex[:12]}-{i}" for i in range(3)]
    native, work, outer = sockets
    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("TMUX", None)
    received = tmp_path / "keys"
    program = tmp_path / "history.py"
    program.write_text(
        "import os,sys,tty\n"
        "tty.setraw(0)\n"
        "os.write(1, ''.join(f'HISTORY_{i:03d}\\r\\n' for i in range(150)).encode())\n"
        "os.write(1, b'> UNSENT_DRAFT')\n"
        "with open(sys.argv[1], 'ab', buffering=0) as stream:\n"
        " while True: stream.write(os.read(0,4096))\n"
    )

    def tmux(socket, *args):
        return subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args],
                              env=env, capture_output=True, text=True, check=True, timeout=5).stdout.strip()

    def position(expected):
        drain()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            actual = tmux(native, "display-message", "-p", "-t", "%0", "#{scroll_position}")
            if actual == str(expected):
                return
            time.sleep(.01)
        assert actual == str(expected)

    def drain(duration=.1):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([master], [], [], .01)[0]:
                os.read(master, 65536)

    master = pid = None
    try:
        tmux(native, "new-session", "-d", "-s", "test", "-x", "90", "-y", "18",
             shlex.join([sys.executable, str(program), str(received)]))
        tmux(work, "new-session", "-d", "-s", "test", "-x", "90", "-y", "18",
             nested_attach_command(native, "test"))
        tmux(outer, "new-session", "-d", "-s", "test", "-x", "90", "-y", "18",
             nested_attach_command(work, "test"))
        for socket in sockets:
            tmux(socket, "set", "-g", "mouse", "on")
            tmux(socket, "set", "-g", "status", "off")
            tmux(socket, "set", "-g", "mode-keys", mode_keys)
            for command in terminal_input_commands():
                tmux(socket, *command)
        focused = tmux(outer, "split-window", "-h", "-l", "20", "-P", "-F", "#{pane_id}", "sleep 60")
        pid, master = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 90, 0, 0))
            os.execvpe("tmux", ["tmux", "-L", outer, "attach-session", "-t", "=test"], env)
        output = b""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and b"?1006h" not in output:
            if select.select([master], [], [], .05)[0]:
                output += os.read(master, 65536)
        assert b"?1006h" in output
        drain(.3)  # let both nested clients finish their initial resize/redraw
        before = tmux(native, "capture-pane", "-p")
        assert "UNSENT_DRAFT" in before

        os.write(master, b"\x1b[<64;10;8M")
        position(1)
        os.write(master, b"\x1b[<64;10;8M" * 60)
        position(61)
        # Inspect what the nested terminal actually rendered to the viewer.
        scrolled = tmux(work, "capture-pane", "-p")
        assert "UNSENT_DRAFT" not in scrolled
        os.write(master, b"\x1b[<65;10;8M" * 61)
        position("")  # copy-mode -e exits automatically at the live bottom
        assert tmux(native, "capture-pane", "-p") == before
        assert tmux(outer, "display-message", "-p", "#{pane_id}") == focused
        assert received.read_bytes() == b""  # no pager toggles or synthetic CLI keys
        for socket in sockets:
            assert tmux(socket, "display-message", "-p", "-t", "%0", "#{pane_in_mode}") == "0"
    finally:
        if master is not None:
            os.close(master)
        if pid:
            os.waitpid(pid, 0)
        for socket in reversed(sockets):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=5)
