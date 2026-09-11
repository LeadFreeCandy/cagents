"""Opt-in QA with installed CLIs, isolated homes, and refused local endpoints.

No real prompt reaches a model service and ordinary runs never touch clipboard.
Run CAGENTS_NATIVE_CLI_TESTS=1 python -m pytest -q tests/live/test_direct_native_clis.py.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

import pytest

from test_direct_input import terminal
from test_direct_panes import direct

pytestmark = pytest.mark.skipif(os.environ.get("CAGENTS_NATIVE_CLI_TESTS") != "1",
                                reason="opt-in installed native CLI QA")


def wait_for(t, pane, predicate, seconds=15):
    end = time.monotonic() + seconds
    text = ""
    while time.monotonic() < end:
        t.drain(.15)
        text = t.d.run(["capture-pane", "-p", "-t", pane])
        if predicate(text):
            return text
    raise AssertionError(text)


def test_installed_codex_scroll_copy_paste_color_and_pane_identity(terminal):
    t, d = terminal, terminal.d
    home = d.path / "native-codex"
    home.mkdir()
    (home / "packages/standalone").mkdir(parents=True)
    current = Path.home() / ".codex/packages/standalone/current"
    if current.exists():
        (home / "packages/standalone/current").symlink_to(current)
    (home / "config.toml").write_text(
        'model="gpt-5.6-terra"\nmodel_provider="probe"\n'
        '[model_providers.probe]\nname="Local QA endpoint"\n'
        'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\n'
        'request_max_retries=0\nstream_max_retries=0\n'
        '[projects.' + json.dumps(str(d.path)) + ']\ntrust_level="trusted"\n')
    binary = str(Path.home() / ".local/bin/codex")
    subprocess.run([binary, "login", "--with-api-key"], input="isolated-test-key\n", text=True,
                   env={**os.environ, "CODEX_HOME": str(home)}, capture_output=True, check=True, timeout=15)
    d.run(["set-environment", "-gu", "NO_COLOR"])
    d.run(["set-environment", "-g", "COLORTERM", "truecolor"])
    name = d.tmux.new_codex_session(str(d.path), ["-C", str(d.path)], "codex:qa-native", home, binary)
    a = next(s for s in d.tmux.list_sessions() if s.name == name)
    d.show(a)
    d.sidecar.focus_session()
    wait_for(t, a.pane_id, lambda s: "gpt-5.6-terra" in s)
    prompt = "\n".join(f"SCROLL_TEST_LINE_{i:02d}" for i in range(50))
    t.write(("\x1b[200~" + prompt + "\x1b[201~").encode())
    t.write(b"\r")
    wait_for(t, a.pane_id, lambda s: "SCROLL_TEST_LINE_49" in s)
    t.drain(1)
    t.write(b"\x1b[200~UNSENT_DRAFT\x1b[201~")
    wait_for(t, a.pane_id, lambda s: "UNSENT_DRAFT" in s)
    colored = d.run(["capture-pane", "-ep", "-t", a.pane_id])
    user_rows = [row for row in colored.splitlines() if "SCROLL_TEST_LINE_" in row]
    assert user_rows and any(re.search(r"\x1b\[[\d;]*48[;:]", row) for row in user_rows), colored
    copied = d.path / "native-copied"
    mac_clipboard = os.environ.get("CAGENTS_MAC_CLIPBOARD_TESTS") == "1"
    copier = ("tee " + shlex.quote(str(copied)) + " | /usr/bin/pbcopy") if mac_clipboard else "cat > " + shlex.quote(str(copied))
    d.run(["set", "-s", "copy-command", copier])
    t.write(t.packet(a.pane_id, 64, 10, 8) * 12)
    offset = d.run(["display-message", "-p", "-t", a.pane_id, "#{scroll_position}"])
    screen = d.run(["capture-pane", "-p", "-S", "-" + (offset or "0"), "-t", a.pane_id]).splitlines()
    # The fixture trims the outer whitespace of command output; use a middle
    # row so its native indentation is preserved for mouse coordinates.
    row, text = next((i, line) for i, line in enumerate(screen[:30]) if i > 0 and "SCROLL_TEST_LINE_" in line)
    token = re.search(r"SCROLL_TEST_LINE_\d+", text)[0]
    x = text.index(token) + 1
    t.write(t.packet(a.pane_id, 0, x, row+1))
    t.write(t.packet(a.pane_id, 32, x+len(token), row+1))
    t.write(t.packet(a.pane_id, 0, x+len(token), row+1, "m"))
    assert copied.read_bytes().strip() == token.encode()
    if mac_clipboard:
        assert subprocess.run(["/usr/bin/pbpaste"], capture_output=True, check=True).stdout.strip() == token.encode()
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{selection_present}"]) == "1"
    t.write("\x1b[200~ PASTE_TEST_α\nPASTE_TEST_β\x1b[201~".encode())
    wait_for(t, a.pane_id, lambda s: all(text in s for text in ("UNSENT_DRAFT", "PASTE_TEST_α", "PASTE_TEST_β")))
    other = d.spawn("codex-qa-other")
    d.show(other)
    d.show(a)
    assert d.visible() == a.pane_id
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_pid}"]) == str(a.pane_pid)
    assert len(d.run(["list-clients", "-F", "#{client_pid}"]).splitlines()) == 1


def test_installed_claude_keeps_native_composer_and_draft_across_selection(terminal):
    t, d = terminal, terminal.d
    home = d.path / "native-claude"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}))
    d.run(["set-environment", "-gu", "NO_COLOR"])
    binary = str(Path.home() / ".local/bin/claude")
    env = ["-e", f"CLAUDE_CONFIG_DIR={home}", "-e", "ANTHROPIC_API_KEY=isolated-test-key",
           "-e", "ANTHROPIC_BASE_URL=http://127.0.0.1:9", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"]
    name = d.tmux.new_claude_session(str(d.path), [], "qa-native-claude", binary, env)
    a = next(s for s in d.tmux.list_sessions() if s.name == name)
    d.show(a)
    d.sidecar.focus_session()
    end = time.monotonic() + 20
    while time.monotonic() < end:
        t.drain(.3)
        text = d.run(["capture-pane", "-p", "-t", a.pane_id])
        if "Yes, I trust this folder" in text:
            t.write(b"\x1b[B\r" if "❯ No, exit" in text else b"\r")
            continue
        if any(message in text for message in ("text style", "custom API key")):
            t.write(b"\r")
            continue
        if "❯" in text and ("Claude Code" in text or "for shortcuts" in text):
            break
    t.write("\x1b[200~UNSENT_DRAFT café\x1b[201~".encode())
    wait_for(t, a.pane_id, lambda s: "UNSENT_DRAFT café" in s)
    other = d.spawn("claude-qa-other")
    d.show(other)
    d.show(a)
    wait_for(t, a.pane_id, lambda s: "UNSENT_DRAFT café" in s)
    assert d.visible() == a.pane_id
    assert d.run(["display-message", "-p", "-t", a.pane_id, "#{pane_pid}"]) == str(a.pane_pid)
