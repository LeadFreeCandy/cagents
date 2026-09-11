"""The same Ctrl-T and diff behavior through native, single-server tabs."""
import json

import pytest

from cagents import direct_ctx as entry
from test_direct_panes import direct
from conftest import init_git_repo


@pytest.fixture
def context(direct, monkeypatch):
    d = direct
    init_git_repo(d.path)
    a = d.spawn("native-context")
    d.show(a)
    path = d.path / "context.json"
    path.write_text(json.dumps(dict(dir=str(d.path), session_id=a.cagents_session_id,
                                   tmux_name=a.name, tmux_socket=d.socket)))
    monkeypatch.setenv("CAGENTS_DIRECT_SOCKET", d.socket)
    return d, a, path


def test_shell_key_toggles_native_terminal_and_conversation(context):
    d, a, path = context
    entry.tmux_entry(["shell", "--context", str(path)])
    assert d.run(["display-message", "-p", "#{window_name}"]) == "term-1"
    shell = d.run(["display-message", "-p", "#{pane_id}"])
    assert shell != a.pane_id
    entry.tmux_entry(["shell", "--context", str(path)])
    assert d.run(["display-message", "-p", "#{pane_id}"]) == a.pane_id


def test_diff_builds_native_pane_and_never_moves_or_restarts_agent(context, monkeypatch):
    d, a, path = context
    monkeypatch.setattr("shutil.which", lambda binary: None if binary == "lazygit" else "/usr/bin/" + binary)
    entry.tmux_entry(["diff", "--context", str(path)])
    assert d.run(["display-message", "-p", "#{window_name}"]) == "diff"
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_pid}"]) == str(a.pane_pid)
    assert d.run(["display-message", "-p", "-t", d.rail, "#{window_name}"]) == "diff"


def test_background_terminal_sync_does_not_steal_focus(context):
    d, a, path = context
    d.sidecar.focus_session()
    entry.tmux_entry(["shell", "--context", str(path), "--no-select"])
    assert d.run(["display-message", "-p", "#{pane_id}"]) == a.pane_id
    assert d.run(["display-message", "-p", "#{window_name}"]) == "session"
    assert d.run(["display-message", "-p", "-t", "=cagents3:term-1.1", "#{@cagents_session_id}"]) == a.cagents_session_id


def test_extra_terminals_stay_before_plus_and_survive_tab_switches(context):
    d, a, path = context
    d.sidecar.ensure_workspace(str(d.path), ctx_prog="/usr/bin/true", context_path=str(path))
    entry.tmux_entry(["new-term", "--context", str(path)])
    second = d.run(["display-message", "-p", "#{pane_id}:#{pane_pid}"])
    entry.tmux_entry(["new-term", "--context", str(path)])
    third = d.run(["display-message", "-p", "#{pane_id}:#{pane_pid}"])
    assert second != third
    visible = d.run(["list-windows", "-F", "#{?@cagents_tab,#W,}"]).split()
    assert visible == ["session", "diff", "term-1", "term-2", "term-3", "new-term"]
    d.sidecar.select_tab("term-2")
    assert d.run(["display-message", "-p", "-t", "=cagents3:term-2.1", "#{pane_id}:#{pane_pid}"]) == second
    d.sidecar.focus_session()
    assert d.visible() == a.pane_id
