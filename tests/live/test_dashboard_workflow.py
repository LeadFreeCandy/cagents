"""Installed-agent QA through n -> shell -> provider, with real dashboard keys.

Run CAGENTS_NATIVE_CLI_TESTS=1 pytest -q tests/live/test_dashboard_workflow.py.
Temporary provider configs use a refused localhost endpoint; no model turn is
submitted. Pane captures and app logs are retained in pytest's temporary output.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from conftest import init_git_repo
from dashboard_harness import Dashboard


pytestmark = pytest.mark.skipif(os.environ.get("CAGENTS_NATIVE_CLI_TESTS") != "1",
                                reason="opt-in installed native CLI dashboard QA")


@pytest.fixture
def dashboard(tmp_path):
    d = Dashboard(tmp_path / "dashboard-artifacts")
    try:
        init_git_repo(d.path)
        claude = d.path / "claude"
        claude.mkdir()
        (claude / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}))
        d.env.update(ANTHROPIC_API_KEY="isolated-test-key", ANTHROPIC_BASE_URL="http://127.0.0.1:9",
                     CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
        codex = d.path / "codex"
        (codex / "packages/standalone").mkdir(parents=True)
        current = Path.home() / ".codex/packages/standalone/current"
        if current.exists():
            (codex / "packages/standalone/current").symlink_to(current)
        config = ('model="gpt-5.6-terra"\nmodel_provider="probe"\n'
                  '[model_providers.probe]\nname="Local QA endpoint"\n'
                  'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\n'
                  'request_max_retries=0\nstream_max_retries=0\n')
        for directory in {str(d.path), str(d.path.resolve())}:
            config += '[projects.' + json.dumps(directory) + ']\ntrust_level="trusted"\n'
        (codex / "config.toml").write_text(config)
        binary = shutil.which("codex")
        assert binary, "installed Codex CLI is required for this explicitly enabled QA"
        subprocess.run([binary, "login", "--with-api-key"], input="isolated-test-key\n",
                       text=True, env=d.env, capture_output=True, check=True, timeout=15)
        d.launch()
        yield d
    finally:
        d.close()


def start_provider(d, provider):
    shell = d.new_shell()
    pending = d.pane(shell, "@cagents_session_id")
    d.send(provider + "\r")
    d.wait(lambda: d.active not in (shell, d.rail)
           and d.pane(d.active, "@cagents_role") == "agent", f"{provider} shim did not open the agent", timeout=30)
    pane = d.active
    end = time.monotonic() + 30
    while time.monotonic() < end:
        d.pump(.25)
        text = d.capture(pane)
        assert d.pane(pane, "pane_dead") == "0", f"{provider} exited during startup:\n{text}"
        if "Yes, I trust this folder" in text:
            d.send(b"\x1b[B\r" if "❯ No, exit" in text else b"\r")
            continue
        if any(message in text for message in ("text style", "custom API key")):
            d.send(b"\r")
            continue
        ready = "gpt-5.6-terra" in text if provider == "codex" else (
            "❯" in text and ("Claude Code" in text or "for shortcuts" in text))
        if ready:
            break
    else:
        d.save_artifacts(provider + "-startup")
        pytest.fail(f"{provider} composer never appeared:\n{text}")
    sid = d.pane(pane, "@cagents_session_id")
    d.wait(lambda: sid in json.loads(d.state.read_text())["sessions"], "new conversation was not tracked")
    if provider == "codex":
        assert sid.startswith("codex:") and sid != pending
        assert pending not in json.loads(d.state.read_text())["sessions"]
    else:
        assert sid == pending
    return pane, sid


def test_codex_scrollback_survives_mouse_motion_in_real_dashboard(dashboard):
    d = dashboard
    pane, _ = start_provider(d, "codex")
    pid = d.pane(pane, "pane_pid")
    # The configured endpoint refuses connections; this only creates local UI
    # history, following the existing installed-Codex scroll/copy QA fixture.
    prompt = "\n".join(f"MOUSE_SCROLL_ROW_{i:02d}" for i in range(50))
    d.send("\x1b[200~" + prompt + "\x1b[201~")
    d.send(b"\r")
    d.wait(lambda: "MOUSE_SCROLL_ROW_49" in d.capture(pane), "Codex did not show scroll fixture")
    d.pump(1)
    d.send("\x1b[200~UNSENT_MOUSE_DRAFT\x1b[201~")
    d.wait(lambda: "UNSENT_MOUSE_DRAFT" in d.capture(pane), "Codex did not accept draft")
    d.send(d.mouse(pane, 64, 10, 8) * 12)
    position = d.pane(pane, "scroll_position")
    assert int(position) >= 12
    for target, x, y in ((pane, 12, 9), (d.rail, 10, 8), (pane, 15, 12)):
        d.send(d.mouse(target, 35, x, y))
        assert d.pane(pane, "pane_in_mode") == "1", "hover left Codex scrollback"
        assert d.pane(pane, "scroll_position") == position, "hover reset Codex scroll position"
        assert d.active == pane and d.pane(pane, "pane_pid") == pid
    d.send("\x1b[200~ café\nsecond line\x1b[201~")
    d.wait(lambda: all(part in d.capture(pane) for part in ("UNSENT_MOUSE_DRAFT", "café", "second line")),
           "paste after scrolling lost the Codex draft")
    assert d.pane(pane, "pane_in_mode") == "0"
    d.assert_no_tmux_messages()
    d.save_artifacts("codex-mouse-scrollback")


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_new_provider_draft_terminal_switch_and_dashboard_reload(dashboard, provider):
    d = dashboard
    pane, sid = start_provider(d, provider)
    pid = d.pane(pane, "pane_pid")
    draft = f"QA_UNSENT_{provider}_café"
    d.send("\x1b[200~" + draft + "\x1b[201~")
    d.wait(lambda: draft in d.capture(pane), "native composer did not accept the draft")
    d.save_artifacts(provider + "-draft")

    # Create another conversation through n, then return through the queue.
    other = d.new_shell()
    assert other != pane
    d.select_session(sid)
    assert d.active == pane and d.pane(pane, "pane_pid") == pid
    assert draft in d.capture(pane)

    # The actual terminal shortcut must open a usable, persistent shell.
    d.send(b"\x14")
    d.wait(lambda: d.tmux("display-message", "-p", "#{window_name}") == "term-1"
           and d.active != d.rail, "Ctrl-T did not open the conversation terminal")
    terminal = d.active
    d.check_shell_input()
    d.send(b"\x14")
    d.wait(lambda: d.active == pane, "Ctrl-T did not return to the conversation")
    assert draft in d.capture(pane)

    # Exit only the dashboard. Relaunch and reselect the same native process.
    d.queue()
    before = d.agents()
    d.send(b"q")
    d.wait(lambda: d.pane(d.rail, "pane_dead") == "1", "q did not close the dashboard")
    d.launch()
    d.select_session(sid)
    assert d.agents() == before
    assert d.active == pane and d.pane(pane, "pane_pid") == pid
    assert draft in d.capture(pane)
    assert d.pane(terminal, "pane_dead") == "0"
    assert len(d.tmux("list-clients", "-F", "#{client_pid}").splitlines()) == 1
    d.save_artifacts(provider + "-reloaded")
