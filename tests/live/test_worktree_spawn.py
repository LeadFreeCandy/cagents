"""Real-transport QA for the auto_worktree setting: press n in the actual
dashboard, type `claude` in the actual shell, and check the conversation the
real CLI opens is running in a real linked worktree.

Claude only, and no model turn: the provider config points at a refused
localhost endpoint. Run with

  CAGENTS_NATIVE_CLI_TESTS=1 .venv/bin/python -m pytest -q tests/live/test_worktree_spawn.py
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import init_git_repo
from dashboard_harness import Dashboard

pytestmark = pytest.mark.skipif(
    os.environ.get("CAGENTS_NATIVE_CLI_TESTS") != "1",
    reason="opt-in installed native CLI dashboard QA",
)


@pytest.fixture
def dashboard(tmp_path):
    """The workflow fixture's Claude half — no Codex install required."""
    d = Dashboard(tmp_path / "worktree-artifacts")
    holder = d.path.parent / (d.path.name + "-worktrees")
    try:
        init_git_repo(d.path)
        subprocess.run(["git", "branch", "-m", "main"], cwd=d.path, capture_output=True)
        claude = d.path / "claude"
        claude.mkdir()
        (claude / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "theme": "dark"})
        )
        d.env.update(
            ANTHROPIC_API_KEY="isolated-test-key",
            ANTHROPIC_BASE_URL="http://127.0.0.1:9",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_AUTOUPDATER="1",
        )
        d.state.write_text(json.dumps({
            "version": 1, "sessions": {},
            "settings": {"desktop_notifications": False, "auto_done_duration": "off",
                         "auto_worktree": True},
        }))
        assert shutil.which("claude"), "installed Claude Code CLI required for this QA"
        d.launch()
        yield d
    finally:
        d.close()
        if holder.exists():
            subprocess.run(["chmod", "-R", "u+w", str(holder)], capture_output=True)
            shutil.rmtree(holder, ignore_errors=True)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


def test_typing_claude_opens_the_real_cli_inside_a_worktree(dashboard):
    d = dashboard
    shell = d.new_shell()
    pending = d.pane(shell, "@cagents_session_id")
    d.send("claude\r")
    d.wait(
        lambda: d.active not in (shell, d.rail)
        and d.pane(d.active, "@cagents_role") == "agent",
        "claude shim did not open the agent",
        timeout=60,
    )
    pane = d.active

    # A brand-new worktree is a directory Claude has never seen, so its
    # trust prompt is part of this path rather than an aside. Each prompt is
    # answered once and then waited out: a pane capture lags the redraw, so
    # re-deciding from a stale frame just toggles the selection back.
    answered = set()
    end = time.monotonic() + 90
    while time.monotonic() < end:
        d.pump(.3)
        text = d.capture(pane)
        assert d.pane(pane, "pane_dead") == "0", f"claude exited during startup:\n{text}"
        if "Yes, I trust this folder" in text and "trust" not in answered:
            answered.add("trust")
            d.send(b"\x1b[B\r")
            continue
        for prompt in ("text style", "custom API key"):
            if prompt in text and prompt not in answered:
                answered.add(prompt)
                d.send(b"\r")
                break
        if "❯" in text and ("Claude Code" in text or "for shortcuts" in text):
            break
    else:
        d.save_artifacts("worktree-startup")
        pytest.fail(f"claude composer never appeared:\n{text}")

    worktree = d.path.parent / (d.path.name + "-worktrees") / "cagents-1"

    # The real CLI process is running in the worktree, not the checkout.
    assert Path(d.pane(pane, "pane_current_path")).resolve() == worktree.resolve()
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "cagents/1"
    assert _git(worktree, "rev-parse", "--git-dir") != _git(worktree, "rev-parse", "--git-common-dir")

    # ...and cagents bookkept it there, so the diff and terminal tabs follow.
    sid = d.pane(pane, "@cagents_session_id")
    assert sid == pending
    d.wait(lambda: sid in json.loads(d.state.read_text())["sessions"],
           "new conversation was not tracked")
    tracked = json.loads(d.state.read_text())
    assert Path(tracked["sessions"][sid]["project_dir"]).resolve() == worktree.resolve()
    assert str(d.path) in tracked["worktree_repos"]

    # The conversation terminal opens in the worktree with no warning.
    d.send(b"\x14")
    d.wait(lambda: d.tmux("display-message", "-p", "#{window_name}") == "term-1"
           and d.active != d.rail, "Ctrl-T did not open the conversation terminal")
    assert Path(d.pane(d.active, "pane_current_path")).resolve() == worktree.resolve()
    d.check_shell_input()
    d.assert_no_tmux_messages()
    d.save_artifacts("worktree-spawn")
