"""Real terminal packets through a single tmux client, never mocked keys."""
import fcntl
import os
import pty
import re
import select
import shlex
import struct
import sys
import termios
import time
from types import SimpleNamespace

import pytest

from test_direct_panes import direct, eventually
from cagents.sidecar import queue_top_binding


@pytest.fixture
def terminal(direct):
    d = direct
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    env.pop("TMUX", None)
    env.pop("NO_COLOR", None)
    pid, master = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 140, 0, 0))
        os.execvpe("tmux", ["tmux", "-L", d.socket, "attach-session", "-t", "=cagents3"], env)

    def drain(duration=.15):
        output = b""
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([master], [], [], .01)[0]:
                data = os.read(master, 65536)
                output += data
                for match in re.finditer(rb"\x1b\](10|11);\?(?:\x07|\x1b\\)", data):
                    color = b"2121/2525/2b2b" if match[1] == b"11" else b"dddd/dddd/dddd"
                    os.write(master, b"\x1b]" + match[1] + b";rgb:" + color + b"\x1b\\")
        return output

    def write(data):
        os.write(master, data)
        return drain()

    def agent(mouse=False):
        received = d.path / ("mouse-input" if mouse else "history-input")
        program = d.path / (received.name + ".py")
        program.write_text(
            "import os,sys,tty\ntty.setraw(0)\n"
            "os.write(1,b'\\x1b[?2004h')\n" +
            ("os.write(1,b'\\x1b[?1000h\\x1b[?1006h')\n" if mouse else "") +
            "os.write(1,''.join(f'COPY_LINE_{i:03d}\\r\\n' for i in range(100)).encode())\n"
            "os.write(1,b'> UNSENT_DRAFT')\n"
            "with open(sys.argv[1],'ab',buffering=0) as stream:\n"
            " while True: stream.write(os.read(0,4096))\n")
        name = d.tmux.new_claude_session(str(d.path), [str(program), str(received)],
                                         "direct-input-" + str(mouse), sys.executable)
        row = next(s for s in d.tmux.list_sessions() if s.name == name)
        d.show(row)
        d.sidecar.focus_session()
        drain(.3)
        return row, received

    def packet(pane, button, x, y, suffix="M"):
        left, top = map(int, d.run(["display-message", "-p", "-t", pane,
                                   "#{pane_left}:#{pane_top}"]).split(":"))
        # Native status line is above the panes, not part of pane_top.
        return f"\x1b[<{button};{left+x};{top+y+1}{suffix}".encode()

    try:
        drain(.4)
        yield SimpleNamespace(d=d, agent=agent, drain=drain, write=write, packet=packet)
    finally:
        os.close(master)
        os.waitpid(pid, 0)


def test_native_wheel_is_forwarded_once_to_hovered_application(terminal):
    t, d = terminal, terminal.d
    a, received = t.agent(mouse=True)
    d.sidecar.focus_rail()
    packets = t.packet(a.pane_id, 64, 10, 8) * 7 + t.packet(a.pane_id, 65, 10, 8) * 4
    t.write(packets)
    assert received.read_bytes() == b"\x1b[<64;10;8M" * 7 + b"\x1b[<65;10;8M" * 4
    assert d.run(["display-message", "-p", "#{pane_id}"]) == d.rail
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_in_mode}"]) == "0"


@pytest.mark.parametrize("mode", ["emacs", "vi"])
def test_selection_survives_release_and_unicode_paste_preserves_draft(terminal, mode):
    t, d = terminal, terminal.d
    a, received = t.agent()
    d.run(["set", "-g", "mode-keys", mode])
    copied = d.path / "copied"
    d.run(["set", "-s", "copy-command", "cat > " + shlex.quote(str(copied))])
    t.write(t.packet(a.pane_id, 64, 10, 8) * 3)
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{scroll_position}"]) == "3"
    # capture-pane reads the backing grid; use the measured history offset
    # to select the fifth row actually displayed by copy-mode.
    offset = d.run(["display-message", "-p", "-t", a.pane_id, "#{scroll_position}"])
    row = d.run(["capture-pane", "-p", "-S", "-" + offset, "-t", a.pane_id]).splitlines()[4]
    assert row.startswith("COPY_LINE_")
    t.write(t.packet(a.pane_id, 0, 1, 5))
    t.write(t.packet(a.pane_id, 32, len(row)+1, 5))
    t.write(t.packet(a.pane_id, 0, len(row)+1, 5, "m"))
    eventually(lambda: copied.exists())
    assert copied.read_bytes().strip() == row.encode()
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_in_mode}:#{selection_present}"]) == "1:1"
    # Switching away and back must not discard the native selection either.
    other = d.spawn("input-other")
    d.show(other)
    d.show(a)
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{selection_present}"]) == "1"
    pasted = "\x1b[200~q\n:restart\nSecond line — café\n\x1b[201~".encode()
    t.write(pasted)
    assert received.read_bytes() == pasted
    assert "UNSENT_DRAFT" in d.tmux.capture_pane(a.name)
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_in_mode}"]) == "0"


def test_ctrl_g_from_zoomed_agent_reaches_queue_only(terminal):
    t, d = terminal, terminal.d
    a, received = t.agent()
    rail_keys = d.path / "rail-keys"
    recorder = d.path / "rail.py"
    recorder.write_text("import os,sys,tty\ntty.setraw(0)\n"
                        "with open(sys.argv[1],'ab',buffering=0) as f:\n"
                        " while True: f.write(os.read(0,4096))\n")
    d.run(["respawn-pane", "-k", "-t", d.rail, shlex.join([sys.executable, str(recorder), str(rail_keys)])])
    d.run(queue_top_binding())
    d.sidecar.hide_rail()
    t.write(b"\x07")
    assert d.run(["display-message", "-p", "#{pane_id}:#{window_zoomed_flag}"]) == d.rail + ":0"
    eventually(lambda: rail_keys.exists() and rail_keys.read_bytes() == b"\x07")
    assert received.read_bytes() == b""


def test_color_queries_reach_terminal_without_startup_helper(terminal):
    t, d = terminal, terminal.d
    result = d.path / "colors"
    query = d.path / "colors.py"
    query.write_text("import os,select,sys,time,tty\ntty.setraw(0)\n"
                     "os.write(1,b'\\x1b]10;?\\x1b\\\\\\x1b]11;?\\x1b\\\\')\n"
                     "end=time.monotonic()+2\ndata=b''\n"
                     "while time.monotonic()<end:\n"
                     " if select.select([0],[],[],.05)[0]: data+=os.read(0,4096)\n"
                     "open(sys.argv[1],'wb').write(data)\ntime.sleep(60)\n")
    name = d.tmux.new_claude_session(str(d.path), [str(query), str(result)], "codex:color-query", sys.executable)
    # Query while the native pane is still parked, just as a CLI starts.
    t.drain(2.3)
    colors = result.read_bytes()
    assert b"10;rgb:dddd/dddd/dddd" in colors
    assert b"11;rgb:2121/2525/2b2b" in colors
    assert len(d.tmux.list_sessions()) == 1


def test_selection_while_zoomed_keeps_the_native_view_zoomed(terminal):
    d = terminal.d
    a, _ = terminal.agent()
    b = d.spawn("native-zoom")
    d.sidecar.hide_rail()
    assert d.run(["display-message", "-p", "#{window_zoomed_flag}"]) == "1"
    d.show(b)
    assert d.visible() == b.pane_id
    assert d.run(["display-message", "-p", "#{window_zoomed_flag}"]) == "1"


@pytest.mark.parametrize("dim", [True, False])
def test_parking_conversations_does_not_flash_tmux_error_messages(terminal, dim):
    from cagents.direct_app import DirectApp
    t, d = terminal, terminal.d
    a, _ = t.agent()
    b = d.spawn("navigation-without-messages")
    app = DirectApp(store=d.app.store, tmux=d.tmux, sidecar=d.sidecar)
    app._apply_dim_chat(dim)
    width = d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"])
    for row in (b, a, b, a):
        d.show(row)
        d.sidecar.focus_session()
        t.drain()
        assert d.run(["show-option", "-pqv", "-t", row.pane_id, "window-style"]) == ""
        d.sidecar.focus_rail()
        t.drain()
        style = d.run(["show-option", "-pqv", "-t", row.pane_id, "window-style"])
        assert style == ("bg=colour234" if dim else "")
        assert d.visible() == row.pane_id
        assert d.run(["display-message", "-p", "-t", row.pane_id, "#{pane_pid}"]) == str(row.pane_pid)
        assert d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"]) == width
    # show-messages records the actual attached client's yellow status bar,
    # which capture-pane cannot see because it is outside the pane's contents.
    messages = [line for line in d.run(["show-messages"]).splitlines() if " message:" in line]
    assert messages == [], "navigation must not display tmux errors: " + repr(messages)


def test_explicit_arrow_size_controls_keep_wide_compact_and_zoom_states(terminal):
    from cagents.direct_app import DirectApp
    t, d = terminal, terminal.d
    a, _ = t.agent()
    app = DirectApp(store=d.app.store, tmux=d.tmux, sidecar=d.sidecar)
    app._apply_arrow_settings()
    app.action_grow_session()
    assert d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"]) == "34"
    t.write(b"\x1b[D")
    assert d.run(["display-message", "-p", "#{pane_id}"]) == d.rail
    assert d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"]) == "70"
    app.action_grow_session()
    t.write(b"\x1b[C")
    assert d.run(["display-message", "-p", "#{window_zoomed_flag}"]) == "1"


def test_terminal_activity_is_attributed_to_its_conversation_only(terminal):
    t, d = terminal, terminal.d
    a, _ = t.agent()
    other = d.spawn("native-inactive")
    d.tmux.ensure_session_window(a.name, "term", str(d.path))
    pane = d.tmux.ensure_window_view(a.name, "term")
    d.sidecar.open_terminal_tab(pane)
    d.sidecar.focus_pane()
    t.write(b" ")
    activity = d.tmux.client_activity()
    assert activity.get(f"{d.socket}:{a.name}", 0) > time.time() - 10
    assert f"{d.socket}:{other.name}" not in activity
