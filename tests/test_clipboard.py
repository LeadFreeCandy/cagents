"""Clipboard and paste round trips through real nested tmux terminals."""
import base64
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from cagents.sidecar import nested_attach_command
from cagents.tmuxctl import terminal_input_commands


@pytest.fixture(params=["emacs", "vi"])
def terminal(tmp_path, request):
    if not shutil.which("tmux"):
        pytest.skip("tmux required")
    import fcntl
    import pty
    import struct
    import termios

    sockets = [f"cg-clipboard-{uuid.uuid4().hex[:10]}-{i}" for i in range(3)]
    native, work, outer = sockets
    received = tmp_path / "input"
    copied_file = tmp_path / "copied"
    copier = tmp_path / "copy.py"
    copier.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_bytes(sys.stdin.buffer.read())\n")
    program = tmp_path / "terminal.py"
    program.write_text(
        "import os,sys,tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'\\x1b[?2004h')\n"
        "os.write(1, ''.join(f'COPY_LINE_{i:03d}\\r\\n' for i in range(100)).encode())\n"
        "os.write(1, b'> UNSENT_DRAFT')\n"
        "with open(sys.argv[1], 'ab', buffering=0) as stream:\n"
        " while True:\n"
        "  data = os.read(0,4096)\n"
        "  stream.write(data)\n"
        "  if data == b'\\x05':\n"
        "   os.write(1, b'\\x1b]52;c;Q0xJX0NPUFlfVEVTVA==\\x07')\n"
    )
    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("TMUX", None)

    def tmux(socket, *args):
        return subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args],
                              env=env, capture_output=True, text=True, check=True, timeout=5).stdout

    master = pid = None

    def drain(duration=.15):
        output = b""
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([master], [], [], .01)[0]:
                output += os.read(master, 65536)
        return output

    def write(data):
        os.write(master, data)
        return drain()

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
            tmux(socket, "set", "-g", "focus-events", "on")
            tmux(socket, "set", "-g", "mode-keys", request.param)
            for command in terminal_input_commands():
                tmux(socket, *command)
            if request.node.originalname != "test_drag_copy_updates_mac_pasteboard":
                # Ordinary tests must not overwrite the developer's clipboard.
                tmux(socket, "set", "-s", "copy-command",
                     shlex.join([sys.executable, str(copier), str(copied_file)]))
        pid, master = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 90, 0, 0))
            os.execvpe("tmux", ["tmux", "-L", outer, "attach-session", "-t", "=test"], env)
        output = drain(.6)
        assert b"?2004h" in output and b"?1006h" in output
        yield SimpleNamespace(tmux=tmux, write=write, drain=drain, received=received,
                              native=native, work=work, outer=outer, sockets=sockets,
                              copied_file=copied_file, directory=tmp_path)
    finally:
        if master is not None:
            os.close(master)
        if pid:
            os.waitpid(pid, 0)
        for socket in reversed(sockets):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=5)


def clipboard_writes(output):
    return [base64.b64decode(payload) for payload in
            re.findall(rb"\x1b\]52;[^;]*;([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)", output)]


def test_native_cli_copy_reaches_outer_terminal(terminal):
    t = terminal
    # A native application's OSC 52 copy must cross all three tmux servers.
    output = t.write(b"\x05")
    assert clipboard_writes(output) == [b"CLI_COPY_TEST"]


def test_drag_copy_from_history_then_multiline_paste(terminal):
    t = terminal
    t.write(b"\x1b[<64;10;8M" * 3)
    assert t.tmux(t.native, "display-message", "-p", "#{scroll_position}").strip() == "3"
    row = t.tmux(t.work, "capture-pane", "-p").splitlines()[4]
    assert row.startswith("COPY_LINE_")
    # Drag from column 1 across the complete fifth row, then release.
    t.write(b"\x1b[<0;1;5M")
    t.write(f"\x1b[<32;{len(row) + 1};5M".encode())
    output = t.write(f"\x1b[<0;{len(row) + 1};5m".encode())
    copied = clipboard_writes(output)
    assert len(copied) == 1 and copied[0].strip() == row.encode()
    # Keeping the highlight after release is now intentional. Preserve this
    # regression's copy/paste checks; only the old auto-cancel expectation changes.
    assert t.tmux(t.native, "display-message", "-p", "#{pane_in_mode}:#{selection_present}").strip() == "1:1"
    payload = copied[0] + "\nSecond line — café\n".encode()
    pasted = b"\x1b[200~" + payload + b"\x1b[201~"
    t.write(pasted)
    assert t.received.read_bytes() == pasted
    assert t.tmux(t.native, "display-message", "-p", "#{pane_in_mode}").strip() == "0"


def test_drag_from_live_output_keeps_highlight_and_copies(terminal):
    t = terminal
    row = t.tmux(t.work, "capture-pane", "-p").splitlines()[4]
    t.write(b"\x1b[<0;1;5M")
    t.write(f"\x1b[<32;{len(row) + 1};5M".encode())
    t.write(f"\x1b[<0;{len(row) + 1};5m".encode())
    assert t.tmux(t.native, "display-message", "-p", "#{pane_in_mode}:#{selection_present}").strip() == "1:1"
    assert t.copied_file.read_bytes().strip() == row.encode()
    t.write(b"q")
    assert t.tmux(t.native, "display-message", "-p", "#{pane_in_mode}").strip() == "0"
    assert t.received.read_bytes() == b""  # q dismisses the selection, not the agent


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("CAGENTS_MAC_CLIPBOARD_TESTS") != "1",
                    reason="explicit macOS pasteboard QA; run under the clipboard-preserving harness")
def test_drag_copy_updates_mac_pasteboard(terminal):
    t = terminal
    subprocess.run(["/usr/bin/pbcopy"], input=b"BEFORE_CAGENTS_COPY", check=True)
    row = t.tmux(t.work, "capture-pane", "-p").splitlines()[4]
    t.write(b"\x1b[<0;1;5M")
    t.write(f"\x1b[<32;{len(row) + 1};5M".encode())
    # The PTY deliberately does not interpret OSC 52. The actual OS clipboard
    # must change anyway, and the selection must remain visible after release.
    t.write(f"\x1b[<0;{len(row) + 1};5m".encode())
    copied = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, check=True).stdout
    assert copied.strip() == row.encode()
    assert t.tmux(t.native, "display-message", "-p", "#{selection_present}").strip() == "1"


def test_paste_exits_history_without_losing_brackets_or_text(terminal):
    t = terminal
    t.write(b"\x1b[<64;10;8M" * 3)
    # Changing focus or hovering the pointer must leave history in place.
    t.write(b"\x1b[O\x1b[I")
    t.write(b"\x1b[<35;10;8M")
    assert t.tmux(t.native, "display-message", "-p", "#{scroll_position}").strip() == "3"
    # These characters would trigger copy-mode/app bindings if treated as keys.
    pasted = b"\x1b[200~q\n:restart\n[[]] \x1b[A\x1b[201~"
    t.write(pasted)
    assert t.received.read_bytes() == pasted
    for socket in t.sockets:
        assert t.tmux(socket, "display-message", "-p", "#{pane_in_mode}").strip() == "0"
