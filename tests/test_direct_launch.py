"""Launch the real Textual dashboard in a PTY and drive its public keys."""
import shutil

import pytest

from dashboard_harness import Dashboard


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_real_dashboard_new_conversation_quit_and_relaunch_preserve_panes(tmp_path):
    d = Dashboard(tmp_path / "dashboard-artifacts")
    try:
        d.launch()
        d.new_shell()  # n must create a live shell that executes typed input
        first = d.agents()
        assert len(first) == 1
        second = d.new_shell()  # Ctrl-G returns from the first shell to the queue
        before = d.agents()
        assert len(before) == 2 and all(row in before for row in first)
        assert d.active == second
        d.queue()
        d.send(b"q")
        d.wait(lambda: d.pane(d.rail, "pane_dead") == "1", "q did not exit the dashboard")
        assert d.agents() == before
        d.launch()
        assert d.pane(d.rail, "pane_dead") == "0"
        assert d.agents() == before
        assert "Traceback" not in d.capture(d.rail)
        assert len(d.tmux("list-clients", "-F", "#{client_pid}").splitlines()) == 1
    finally:
        d.close()
