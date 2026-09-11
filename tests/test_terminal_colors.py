import shlex
import subprocess

import pytest

from cagents import terminal_colors


@pytest.mark.parametrize("reply, expected", [
    (b"\x1b]11;rgb:2121/2525/2b2b\x1b\\", {"11": "#21252b"}),
    (b"\x1b]10;rgb:dd/dd/dd\x07", {"10": "#dddddd"}),
    (b"\x1b]11;rgb:f/0/8\x07", {"11": "#ff0088"}),
    (b"\x1b]11;rgb:ee/ee/ee\x1b", {}),
    (b"\x1b]11;rgb:zz/00/00\x07", {}),
])
def test_terminal_color_reply_formats(reply, expected):
    assert terminal_colors.parse_colors(reply) == expected


@pytest.mark.parametrize("prefix", ["", "exec "])
def test_palette_setup_preserves_exec_and_literal_arguments(monkeypatch, prefix):
    monkeypatch.setattr(terminal_colors, "terminal_colors", lambda _: {"11": "#21252b", "10": "#dddddd"})
    # Exercise the shell, including resume commands which already start with
    # exec, and arguments containing shell syntax that must stay literal.
    launch = prefix + shlex.join(["/usr/bin/printf", "%s", "hello $(false) 'world'"])
    command = terminal_colors.prepare_command(launch)
    output = subprocess.run(["/bin/sh", "-c", command], capture_output=True, check=True).stdout
    assert output == b"\x1b]11;#21252b\x1b\\\x1b]10;#dddddd\x1b\\hello $(false) 'world'"


def test_launch_unchanged_without_a_parent_terminal(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert terminal_colors.prepare_command("codex resume abc") == "codex resume abc"
