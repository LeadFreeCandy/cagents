"""Desktop notifications: the terminal-notifier path, and clicking one
should both activate the terminal app and (via the existing select-request
file) select the session in cagents."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cagents.notifier import notify_desktop, read_select_request


def test_activates_the_terminal_app_when_recognized(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
    args = run.call_args[0][0]
    assert "-activate" in args
    assert args[args.index("-activate") + 1] == "com.googlecode.iterm2"


def test_no_activate_flag_for_unknown_or_missing_term_program(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
    assert "-activate" not in run.call_args[0][0]

    monkeypatch.setenv("TERM_PROGRAM", "some_future_terminal_we_dont_know")
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
    assert "-activate" not in run.call_args[0][0]


def test_apple_terminal_and_ghostty_map_to_their_bundle_ids(tmp_path: Path, monkeypatch):
    for term_program, bundle_id in (
        ("Apple_Terminal", "com.apple.Terminal"),
        ("ghostty", "com.mitchellh.ghostty"),
        ("WezTerm", "com.github.wez.wezterm"),
    ):
        monkeypatch.setenv("TERM_PROGRAM", term_program)
        with patch("subprocess.run") as run:
            notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
        args = run.call_args[0][0]
        assert args[args.index("-activate") + 1] == bundle_id


def test_sender_is_set_so_notification_is_branded_as_the_terminal_not_script_editor(
    tmp_path: Path, monkeypatch
):
    # Plain osascript has no sender override at all (always "Script
    # Editor") — this is specifically why -sender needs terminal-notifier.
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
    args = run.call_args[0][0]
    assert args[args.index("-sender") + 1] == "com.mitchellh.ghostty"


def test_no_sender_flag_for_unknown_term_program(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "some_future_terminal_we_dont_know")
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
    assert "-sender" not in run.call_args[0][0]


def test_click_still_writes_the_select_request(tmp_path: Path, monkeypatch):
    # -execute still fires the same shell snippet regardless of -activate;
    # clicking selects the conversation the way it always has.
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="/usr/bin/terminal-notifier")
    args = run.call_args[0][0]
    execute = args[args.index("-execute") + 1]
    assert "sid123" in execute

    # simulate what -execute does when actually clicked
    (tmp_path / "select-request").write_text("sid123\n")
    assert read_select_request(tmp_path) == "sid123"


def test_osascript_fallback_when_terminal_notifier_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    with patch("subprocess.run") as run:
        notify_desktop("cagents", "hi", "sid123", tmp_path, tn_bin="")
    args = run.call_args[0][0]
    assert args[0] == "osascript"  # no click/activate support without terminal-notifier
