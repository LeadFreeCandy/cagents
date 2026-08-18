"""Full-flow tests against a *real* tmux server (not FakeTmux).

The mocked-tmux unit tests exercise cagents' own logic but can't catch bugs
in the actual mechanism that ties a Claude session to a tmux pane — the
CAGENTS_SESSION_ID env var round-trip through real `tmux` subprocesses. That
mechanism is exactly what two user-reported bugs turned on (a "just
created" session reading as dead, and mashing attach spawning duplicate
`claude --resume` processes), so it gets its own real-server coverage here.

Every test runs on a throwaway `-L` socket so it can't collide with any
real cagents/claude tmux server on the machine, and the server is killed in
a fixture finalizer even if the test fails.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from cagents.sessions import derive_state, map_tmux_sessions
from cagents.store import TrackedSession
from cagents.tmuxctl import TmuxClient

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def real_tmux():
    socket = f"cagents-pytest-{uuid.uuid4().hex[:10]}"
    client = TmuxClient(socket=socket)
    yield client
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    """A stand-in for the real `claude` binary: stays alive so the tmux
    session doesn't immediately exit, without needing the real CLI (or its
    network/auth) in a test."""
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\nsleep 60\n")
    script.chmod(0o755)
    return str(script)


class TestRealTmuxSessionMapping:
    def test_cagents_session_id_resolves_immediately_via_real_tmux(
        self, real_tmux: TmuxClient, tmp_path: Path, fake_claude: str
    ):
        """The exact mechanism `_resume_dead_session`/`_new_session_chosen`
        rely on: a session started with `session_id=...` must show up with
        that id in `list_sessions()`, and `map_tmux_sessions` must resolve
        it — even with `parsed=None` (no transcript written yet), which is
        the state a session is in for its first moments alive."""
        session_id = str(uuid.uuid4())
        directory = str(tmp_path)
        name = real_tmux.new_claude_session(
            directory, ["--session-id", session_id], session_id=session_id,
            claude_bin=fake_claude,
        )

        tmux_sessions = real_tmux.list_sessions()
        matched = [s for s in tmux_sessions if s.name == name]
        assert matched, f"tmux session {name!r} not in list_sessions() at all"
        assert matched[0].cagents_session_id == session_id, (
            "CAGENTS_SESSION_ID round-trip through real tmux failed — "
            f"got {matched[0].cagents_session_id!r}"
        )

        tracked = TrackedSession(session_id=session_id, project_dir=directory, added_at="x")
        mapping = map_tmux_sessions([(tracked, None)], tmux_sessions)
        assert session_id in mapping, (
            "tier-1 (env var) matching failed against a real tmux server "
            "with parsed=None — a brand-new session would read as dead"
        )
        assert mapping[session_id].name == name

    def test_full_flow_new_session_state_is_working_not_missing(
        self, real_tmux: TmuxClient, tmp_path: Path, fake_claude: str
    ):
        """End-to-end: create a session exactly as `_new_session_chosen`
        does, map it against real tmux, and derive its state — must never
        be STOPPED/"transcript missing" while the process is actually
        running. This is the exact bug reported as "it says it could not
        find the data for it even though I just created the session"."""
        session_id = str(uuid.uuid4())
        directory = str(tmp_path)
        real_tmux.new_claude_session(
            directory, ["--session-id", session_id], session_id=session_id,
            claude_bin=fake_claude,
        )
        tracked = TrackedSession(session_id=session_id, project_dir=directory, added_at="x")
        tmux_sessions = real_tmux.list_sessions()
        mapping = map_tmux_sessions([(tracked, None)], tmux_sessions)
        live = session_id in mapping

        state, detail = derive_state(None, tracked, live=live)
        assert live is True
        assert state.value != "stopped", (
            f"a freshly-created live session derived state={state!r} detail={detail!r}"
        )

    def test_resuming_a_session_twice_creates_two_distinct_tmux_sessions(
        self, real_tmux: TmuxClient, tmp_path: Path, fake_claude: str
    ):
        """Documents what app.py's `_recently_started` guard exists to
        prevent: `TmuxClient.new_claude_session` itself has no dedup — call
        it twice for the same Claude session id and real tmux happily
        creates two independent tmux sessions, both attachable, both
        claiming the same CAGENTS_SESSION_ID. That's the mechanism behind
        the observed flicker (two `claude --resume <id>` processes racing
        on one Claude session) once app.py's guard doesn't intervene."""
        session_id = str(uuid.uuid4())
        directory = str(tmp_path)
        name1 = real_tmux.new_claude_session(
            directory, ["--resume", session_id], session_id=session_id,
            claude_bin=fake_claude,
        )
        name2 = real_tmux.new_claude_session(
            directory, ["--resume", session_id], session_id=session_id,
            claude_bin=fake_claude,
        )
        assert name1 != name2
        tmux_sessions = real_tmux.list_sessions()
        claimants = [s for s in tmux_sessions if s.cagents_session_id == session_id]
        assert len(claimants) == 2, "real tmux allows two live sessions to claim one Claude id"
