"""Ground-truth test against a REAL Claude Code session — not synthetic
fixtures. Spawns a real `claude --model haiku` process under cagents' own
hook wiring, drives it through a real permission prompt, a real turn
completion, and (crucially) a real idle-nudge notification, and asserts
`derive_state` reads Claude Code's *actual* hook payloads correctly.

This exists because the synthetic-fixture tests in test_states_and_settings
missed a real bug: Claude Code fires its Notification hook with the exact
same generic message for a real blocking dialog and for a plain "still
idle" nudge, and only the real CLI's `notification_type` field tells them
apart. A hand-written fixture is only as good as the guess behind it —
this test asks the real binary instead of guessing.

Costs real API tokens (haiku, a handful of tiny turns) and takes ~2
minutes of real wall-clock time (it waits out Claude Code's own ~60s idle
threshold). Skipped by default. Run explicitly:

    CAGENTS_LIVE_TESTS=1 pytest tests/live/test_live_state_detection.py -v -s

Requires the `claude` CLI on PATH and authenticated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cagents.claude_data import encode_project_dir, parse_session_file
from cagents.sessions import SessionState, derive_state
from cagents.store import TrackedSession

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAGENTS_LIVE_TESTS"),
    reason="live test: spawns a real Claude Code process, costs tokens, "
    "takes ~2 minutes. Run with CAGENTS_LIVE_TESTS=1 to opt in.",
)

SOCKET = "cagents-live-test"
IDLE_WAIT_SECONDS = 90  # Claude Code's own idle_prompt threshold is ~60s


def _tmux(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True)


def _capture() -> str:
    return _tmux("capture-pane", "-p", "-t", "probe").stdout


def _read_events(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


@pytest.fixture
def live_probe(tmp_path: Path):
    claude_bin = shutil.which("claude")
    ctx_bin = shutil.which("cagents-ctx") or str(
        Path(sys.executable).parent / "cagents-ctx"
    )
    assert claude_bin, "claude CLI not found on PATH"

    project_dir = tmp_path / "live_project"
    project_dir.mkdir()
    events_file = tmp_path / "events.json"
    session_id = str(uuid.uuid4())

    _tmux("kill-server")
    settings = {
        "hooks": {
            k: [{"hooks": [{"type": "command", "command": f"{ctx_bin} event {k} --file {events_file}"}]}]
            for k in ("Notification", "Stop", "UserPromptSubmit")
        }
    }
    cmd = [claude_bin, "--model", "haiku", "--session-id", session_id, "--settings", json.dumps(settings)]
    _tmux("new-session", "-d", "-s", "probe", "-x", "220", "-y", "50", "-c", str(project_dir), *cmd)
    time.sleep(4)
    if "trust this folder" in _capture():
        _tmux("send-keys", "-t", "probe", "1", "Enter")
        time.sleep(3)

    transcript = (
        Path.home() / ".claude" / "projects" / encode_project_dir(str(project_dir)) / f"{session_id}.jsonl"
    )
    try:
        yield project_dir, events_file, session_id, transcript
    finally:
        _tmux("kill-server")
        if transcript.exists():
            shutil.rmtree(transcript.parent, ignore_errors=True)


def _state(project_dir: Path, events_file: Path, session_id: str, transcript: Path):
    events = _read_events(events_file)
    parsed = parse_session_file(transcript) if transcript.exists() else None
    if parsed is None:
        return None, None, events
    tracked = TrackedSession(session_id, str(project_dir), "2026-01-01T00:00:00+00:00")
    state, detail = derive_state(
        parsed, tracked, live=True, pane_text=_capture(), now=time.time(), events=events
    )
    return state, detail, events


def test_real_session_permission_stop_and_idle_nudge(live_probe):
    project_dir, events_file, session_id, transcript = live_probe

    # A command "rm" is on Claude Code's own needs-approval list even for
    # a harmless target it just created — unlike a plain read, which
    # auto-runs and would never exercise the permission_prompt path.
    _tmux(
        "send-keys", "-t", "probe",
        "Create a file junk.txt with 'x' in it, then run: rm junk.txt", "Enter",
    )

    deadline = time.time() + 30
    events = {}
    while time.time() < deadline:
        time.sleep(2)
        _, _, events = _state(project_dir, events_file, session_id, transcript)
        if events.get("notification_type") == "permission_prompt":
            break
    assert events.get("notification_type") == "permission_prompt", (
        "never observed a real permission_prompt notification — "
        f"got events={events}, pane=\n{_capture()}"
    )
    state, detail, _ = _state(project_dir, events_file, session_id, transcript)
    assert state == SessionState.NEEDS_INPUT, f"expected NEEDS_INPUT, got {state} ({detail})"

    # Approve the numbered-choice dialog ("❯ 1. Yes") and let the turn finish.
    _tmux("send-keys", "-t", "probe", "1")
    time.sleep(1)
    _tmux("send-keys", "-t", "probe", "Enter")

    deadline = time.time() + 30
    state = None
    while time.time() < deadline:
        time.sleep(2)
        state, detail, events = _state(project_dir, events_file, session_id, transcript)
        if events.get("Stop"):
            break
    assert events.get("Stop"), f"turn never completed — events={events}"
    assert state == SessionState.NEEDS_REVIEW, f"expected NEEDS_REVIEW after Stop, got {state}"

    # THE ACTUAL BUG: sit idle until Claude Code's own idle_prompt fires,
    # and confirm it does NOT flip the state back to NEEDS_INPUT.
    deadline = time.time() + IDLE_WAIT_SECONDS
    saw_idle = False
    while time.time() < deadline:
        time.sleep(4)
        state, detail, events = _state(project_dir, events_file, session_id, transcript)
        if events.get("notification_type") == "idle_prompt":
            saw_idle = True
            break
    if not saw_idle:
        pytest.skip("no idle_prompt notification observed in the wait window (CLI-timing dependent)")
    assert state == SessionState.NEEDS_REVIEW, (
        f"BUG: a real idle_prompt notification flipped state to {state} "
        "instead of staying NEEDS_REVIEW"
    )
