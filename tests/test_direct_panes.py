"""Observable pane contracts, initially reproduced against the legacy backend.

The fixture wiring changes with the transport; pane/process/draft assertions do
not. Every server in these tests is isolated from the user's live conversations.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from cagents.app import CagentsApp
from cagents.direct import DirectSidecar as Sidecar, setup_commands as container_setup_commands
from cagents.store import Store
from cagents.direct import DirectTmux as TmuxClient


def eventually(fn, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.02)
    assert fn(), "condition never became true"


@pytest.fixture
def direct(tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux required")
    socket = "cg-direct-" + uuid.uuid4().hex[:12]
    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("TMUX", None)

    def run(args):
        p = subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", *args],
                           capture_output=True, text=True, env=env, timeout=5)
        if p.returncode:
            raise RuntimeError(p.stderr.strip())
        return p.stdout.strip()

    try:
        rail = run(["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "cagents3",
                    "-n", "session", "-x", "140", "-y", "35", "sleep 120"])
        tmux = TmuxClient(sockets=(socket,), create_socket=socket)
        sidecar = Sidecar(runner=run, own_pane=rail, client=tmux)
        sidecar.ensure_workspace(terminal_dir=str(tmp_path))
        app = CagentsApp(store=Store.load(tmp_path / "state.json"), tmux=tmux, sidecar=sidecar)
        agent = tmp_path / "agent.py"
        agent.write_text("import os,tty\ntty.setraw(0)\nos.write(1,b'NATIVE_READY UNSENT_DRAFT')\n"
                         "while True: os.write(1,os.read(0,4096))\n")

        def spawn(sid):
            name = tmux.new_claude_session(str(tmp_path), [str(agent)], sid, sys.executable)
            row = next(s for s in tmux.list_sessions() if s.name == name)
            eventually(lambda: "NATIVE_READY" in tmux.capture_pane(name))
            return row

        def show(row):
            view = SimpleNamespace(tmux_socket=socket, tmux_name=row.name)
            sidecar.show_viewer(app._viewer_command(view))

        def visible():
            return run(["display-message", "-p", "-t", "=cagents3:session.1", "#{pane_id}"])

        yield SimpleNamespace(run=run, socket=socket, rail=rail, tmux=tmux, sidecar=sidecar,
                              app=app, spawn=spawn, show=show, visible=visible, path=tmp_path)
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, timeout=5)


def test_display_is_the_agent_pane_and_survives_a_b_a_selection(direct):
    d = direct
    a, b = d.spawn("native-a"), d.spawn("native-b")
    for row in (a, b, a):
        d.show(row)
        assert d.visible() == row.pane_id, "viewer must be the native pane, not a tmux attach process"
        assert int(d.run(["display-message", "-p", "-t", row.pane_id, "#{pane_pid}"])) == row.pane_pid
        assert "UNSENT_DRAFT" in d.tmux.capture_pane(row.name)
        discovered = next(s for s in d.tmux.list_sessions() if s.name == row.name)
        assert (discovered.pane_id, discovered.pane_pid) == (row.pane_id, row.pane_pid)
    assert d.run(["list-clients", "-F", "#{client_pid}"]) == "", "no background attachment clients"


def test_focus_does_not_resize_native_content(direct):
    d = direct
    # Reproduce the old focus-driven geometry change as installed at startup.
    for command in container_setup_commands():
        d.run(command)
    d.show(d.spawn("native-focus"))
    width = d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"])
    d.sidecar.focus_session()
    d.sidecar.focus_rail()
    assert d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"]) == width
    # No deferred second resize after focus.
    d.sidecar.focus_session()
    assert d.run(["display-message", "-p", "-t", d.rail, "#{pane_width}"]) == width


def test_queue_and_agent_are_native_panes_across_tabs(direct):
    d = direct
    a = d.spawn("native-tabs")
    d.show(a)
    for tab in ("diff", "term-1", "session"):
        d.sidecar.select_tab(tab)
        assert d.run(["display-message", "-p", "-t", d.rail, "#{window_name}"]) == tab
        assert d.run(["display-message", "-p", "-t", d.rail, "#{pane_index}"]) == "0"
        assert int(d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_pid}"])) == a.pane_pid
    assert d.visible() == a.pane_id


def test_terminal_is_a_persistent_native_pane_scoped_to_each_conversation(direct):
    d = direct
    a, b = d.spawn("native-terminal-a"), d.spawn("native-terminal-b")
    terminals = {}
    for row in (a, b, a):
        d.tmux.ensure_session_window(row.name, "term", str(d.path))
        target = d.tmux.ensure_window_view(row.name, "term")
        view = SimpleNamespace(tmux_socket=d.socket, tmux_name=target)
        d.sidecar.open_terminal_tab(d.app._viewer_command(view))
        pane = d.run(["display-message", "-p", "-t", "=cagents3:term-1.1", "#{pane_id}"])
        command = d.run(["display-message", "-p", "-t", pane, "#{pane_current_command}"])
        assert command != "tmux", "terminal tabs must use their native shell pane"
        marker = d.path / (row.name + "-terminal-input")
        d.run(["send-keys", "-t", pane, f"printf '%s' SHELL_READY > {shlex.quote(str(marker))}", "Enter"])
        eventually(marker.exists)
        assert marker.read_text() == "SHELL_READY"
        assert d.run(["display-message", "-p", "-t", pane, "#{pane_dead}"]) == "0"
        terminals.setdefault(row.name, pane)
        assert terminals[row.name] == pane
    assert terminals[a.name] != terminals[b.name]


@pytest.mark.parametrize("default_command", ["", "export CAGENTS_SHELL_DEFAULT=configured; exec /bin/sh"])
def test_new_conversation_shell_accepts_input_and_keeps_environment(direct, default_command):
    d = direct
    d.run(["set", "-g", "default-shell", "/bin/sh"])
    d.run(["set", "-g", "default-command", default_command])
    name = d.tmux.new_shell_session(str(d.path), "native-new-shell",
                                   ["-e", "CAGENTS_TEST_SHELL=kept"])
    pane = d.tmux.pane(name)
    marker = d.path / "new-shell-input"
    d.tmux.send_shell_command(name, f'printf "%s" "$CAGENTS_SESSION_ID:$CAGENTS_TEST_SHELL:${{CAGENTS_SHELL_DEFAULT-unset}}" > {shlex.quote(str(marker))}')
    eventually(marker.exists)
    assert marker.read_text() == "native-new-shell:kept:" + ("configured" if default_command else "unset")
    assert d.run(["display-message", "-p", "-t", pane, "#{pane_dead}"]) == "0"
    # macOS implements /bin/sh with bash and tmux reports the executable name.
    assert d.run(["display-message", "-p", "-t", pane, "#{pane_current_command}"]) in {"sh", "bash"}


def test_parking_slots_do_not_keep_placeholder_processes_alive(direct):
    d = direct
    a = d.spawn("native-idle-memory")
    d.show(a)
    home = d.run(["display-message", "-p", "-t", a.pane_id, "#{@cagents_home}"])
    assert d.run(["display-message", "-p", "-t", home, "#{pane_dead}"]) == "1"


def test_restart_codex_uses_native_terminal_without_color_probe(direct, monkeypatch):
    d = direct
    a = d.spawn("codex:native-restart")
    d.show(a)
    def unexpected_probe(*args, **kwargs):
        pytest.fail("a direct agent must not spawn another terminal to relay colors")
    monkeypatch.setattr("cagents.terminal_colors.prepare_command", unexpected_probe)
    d.tmux.replace_agent(a.name, a.cagents_session_id, socket=d.socket,
                         pane_id=a.pane_id, pane_pid=a.pane_pid,
                         command=__import__("shlex").join([sys.executable, str(d.path / "agent.py")]))
    assert d.visible() == a.pane_id
    updated = next(s for s in d.tmux.list_sessions() if s.name == a.name)
    assert updated.pane_pid != a.pane_pid


def test_suspend_and_restart_preserve_terminal_and_reject_stale_identity(direct):
    d = direct
    a = d.spawn("native-lifecycle")
    d.show(a)
    d.tmux.ensure_session_window(a.name, "term", str(d.path))
    term = d.tmux.ensure_window_view(a.name, "term")
    pid = d.run(["display-message", "-p", "-t", term, "#{pane_pid}"])
    d.tmux.replace_agent(a.name, a.cagents_session_id, socket=d.socket,
                         pane_id=a.pane_id, pane_pid=a.pane_pid)
    updated = next(s for s in d.tmux.list_sessions() if s.name == a.name)
    assert updated.suspended and updated.pane_dead
    assert d.run(["display-message", "-p", "-t", term, "#{pane_pid}"]) == pid
    with pytest.raises(RuntimeError, match="changed"):
        d.tmux.replace_agent(a.name, a.cagents_session_id, socket=d.socket,
                             pane_id=a.pane_id, pane_pid=a.pane_pid)
    d.tmux.replace_agent(a.name, a.cagents_session_id, socket=d.socket,
                         pane_id=a.pane_id, pane_pid=updated.pane_pid,
                         command=__import__("shlex").join([sys.executable, str(d.path / "agent.py")]))
    eventually(lambda: "NATIVE_READY" in d.tmux.capture_pane(a.name))
    assert d.visible() == a.pane_id
    assert not next(s for s in d.tmux.list_sessions() if s.name == a.name).suspended
    assert d.run(["display-message", "-p", "-t", term, "#{pane_pid}"]) == pid


def test_dashboard_reconstruction_preserves_display_and_terminal(direct):
    d = direct
    a = d.spawn("native-reload")
    d.show(a)
    before = d.run(["list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"])
    again = Sidecar(runner=d.run, own_pane=d.rail, client=d.tmux)
    again.ensure_workspace(str(d.path))
    again.show_viewer(d.app._viewer_command(SimpleNamespace(tmux_socket=d.socket, tmux_name=a.name)))
    assert d.visible() == a.pane_id
    assert d.run(["list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"]) == before


def test_repository_named_like_a_tab_does_not_confuse_native_targets(direct):
    d = direct
    directory = d.path / "session"
    directory.mkdir()
    name = d.tmux.new_claude_session(str(directory), ["-c", "import time; time.sleep(60)"],
                                     "native-reserved-name", sys.executable)
    row = next(s for s in d.tmux.list_sessions() if s.name == name)
    assert name not in ("session", "diff", "term-1", "new-term")
    d.show(row)
    assert d.visible() == row.pane_id


def test_native_spawn_preserves_provider_and_shim_environment(direct):
    d = direct
    import json
    result = d.path / "environment.json"
    code = "import json,os,time; open(" + repr(str(result)) + ", 'w').write(json.dumps(dict(os.environ))); time.sleep(60)"
    d.tmux.new_claude_session(str(d.path), ["-c", code], "native-environment", sys.executable,
                             ["-e", "CODEX_HOME=/isolated/codex", "-e", "ZDOTDIR=/isolated/shims"])
    eventually(result.exists)
    environment = json.loads(result.read_text())
    assert environment["CODEX_HOME"] == "/isolated/codex"
    assert environment["ZDOTDIR"] == "/isolated/shims"
    assert environment["CAGENTS_SESSION_ID"] == "native-environment"


def test_restart_retains_the_original_native_provider_environment(direct):
    d = direct
    import json
    import shlex
    result = d.path / "restart-env.json"
    code = "import json,os,time; open(" + repr(str(result)) + ", 'w').write(json.dumps(dict(os.environ))); time.sleep(60)"
    name = d.tmux.new_claude_session(str(d.path), ["-c", code], "codex:native-restart-env", sys.executable,
                                     ["-e", "CODEX_HOME=/isolated/provider", "-e", "ZDOTDIR=/isolated/terminal"])
    eventually(result.exists)
    assert json.loads(result.read_text())["CODEX_HOME"] == "/isolated/provider"
    result.unlink()
    a = next(s for s in d.tmux.list_sessions() if s.name == name)
    d.tmux.replace_agent(name, a.cagents_session_id, socket=d.socket, pane_id=a.pane_id,
                         pane_pid=a.pane_pid, command=shlex.join([sys.executable, "-c", code]))
    eventually(result.exists)
    environment = json.loads(result.read_text())
    assert environment["CODEX_HOME"] == "/isolated/provider"
    assert environment["ZDOTDIR"] == "/isolated/terminal"
    assert environment["CAGENTS_SESSION_ID"] == a.cagents_session_id


def test_foreign_agent_uses_one_attachment_hop_and_keeps_its_process(direct):
    d = direct
    import shlex
    foreign = d.socket + "-existing"
    def old(*args):
        return subprocess.check_output(["tmux", "-L", foreign, "-f", "/dev/null", *args], text=True).strip()
    try:
        pane = old("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "existing",
                   shlex.join([sys.executable, str(d.path / "agent.py")]))
        pid = old("display-message", "-p", "-t", pane, "#{pane_pid}")
        own = d.spawn("native-alongside-old")
        for _ in range(2):
            command = d.app._viewer_command(SimpleNamespace(tmux_socket=foreign, tmux_name="existing"))
            d.sidecar.show_viewer(command)
            tty = d.run(["display-message", "-p", "-t", "=cagents3:session.1", "#{pane_tty}"])
            eventually(lambda: old("list-clients", "-F", "#{client_tty}") == tty)
            assert old("display-message", "-p", "-t", pane, "#{pane_pid}") == pid
            d.show(own)
            assert d.visible() == own.pane_id
        assert old("display-message", "-p", "-t", pane, "#{pane_pid}") == pid
    finally:
        subprocess.run(["tmux", "-L", foreign, "kill-server"], capture_output=True, timeout=5)
