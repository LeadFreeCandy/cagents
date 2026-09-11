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
