"""Launch the real Textual dashboard in a PTY and drive its public keys."""
import shutil

import pytest

from dashboard_harness import Dashboard


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_real_dashboard_mouse_motion_keeps_native_scrollback(tmp_path):
    d = Dashboard(tmp_path / "dashboard-artifacts")
    try:
        d.launch()
        pane = d.new_shell()
        d.send("for i in {1..100}; do echo SCROLL_ROW_$i; done\r")
        d.wait(lambda: "SCROLL_ROW_100" in d.capture(pane), "shell did not fill scrollback")
        assert d.pane(d.rail, "mouse_all_flag") == "1"
        pid = d.pane(pane, "pane_pid")
        d.send(d.mouse(pane, 64, 10, 8) * 7)
        assert d.pane(pane, "scroll_position") == "7"
        for target, x, y in ((pane, 12, 9), (d.rail, 10, 8), (pane, 15, 12)):
            d.send(d.mouse(target, 35, x, y))
            assert d.pane(pane, "pane_in_mode") == "1", "hover left native scrollback"
            assert d.pane(pane, "scroll_position") == "7", "hover reset the scroll position"
            assert d.active == pane and d.pane(pane, "pane_pid") == pid
        d.assert_no_tmux_messages()
    finally:
        d.close()


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
