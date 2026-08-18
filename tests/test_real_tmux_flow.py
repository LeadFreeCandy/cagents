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


def test_default_socket_is_not_shared_with_the_users_other_claude_usage():
    """Regression guard for the actual root cause of "flickers/can't find
    session, completely unusable": starting a second real `claude` process
    on a tmux socket that already hosts a live one crashes it every time
    (Bun ENOENT — see tmuxctl.py's module docstring and DECISIONS.md).
    cagents' default socket must never be the conventional "claude" socket
    a user's own wrapper might already have a live session on. Deliberately
    a cheap static check, not a live collision repro: reproducing the crash
    needs two real `claude` processes and is exercised manually, not as a
    routine (and CI-flaky, environment-dependent) test."""
    assert TmuxClient().socket != "claude"


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


class TestRealClaudeCliResumeFailure:
    """The actual root cause of the reported "flickers and doesn't open,
    can't find session" bug: the real `claude` binary, asked to resume a
    session id it has no record of, prints an error and exits (measured:
    ~1.2s of real startup work first) — which tears down its hosting tmux
    session before cagents can attach to it. Skipped (not just
    tmux-gated) when `claude` itself isn't on PATH, since this drives the
    real CLI.

    Deliberately does NOT use pytest's `tmp_path`: an unfamiliar directory
    makes claude show its folder-trust prompt *before* it ever gets to
    validating --resume, so it just sits there waiting — a different code
    path entirely, not the one this test is for. CWD must be a directory
    the local claude install already trusts; run from a real project of
    yours if this fails "claude was expected to have already exited"."""

    pytestmark = pytest.mark.skipif(
        shutil.which("claude") is None, reason="claude CLI not installed"
    )

    TRUSTED_CWD = str(Path.home() / "src")

    def test_resuming_an_unknown_session_id_kills_its_own_tmux_session(
        self, real_tmux: TmuxClient
    ):
        if not Path(self.TRUSTED_CWD).is_dir():
            pytest.skip(f"{self.TRUSTED_CWD} doesn't exist on this machine")
        claude_bin = shutil.which("claude")
        bogus_id = str(uuid.uuid4())  # never seen by claude -> "no conversation found"
        name = real_tmux.new_claude_session(
            self.TRUSTED_CWD, ["--resume", bogus_id], session_id=bogus_id, claude_bin=claude_bin,
        )
        error = real_tmux.wait_for_alive_or_error(name)
        assert error, "claude was expected to have already exited by now"
        assert not real_tmux.has_session(name)


class TestWaitForAliveOrError:
    """Fast, non-tmux unit coverage of the polling primitive itself."""

    class _StubClient:
        def __init__(self, alive_for_n_checks: int, pane_text: str = ""):
            self.remaining = alive_for_n_checks
            self.pane_text = pane_text
            self.has_session_calls = 0

        def has_session(self, name: str) -> bool:
            self.has_session_calls += 1
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True

        def capture_pane(self, name: str, lines: int = 40) -> str:
            return self.pane_text

    def test_still_alive_returns_empty_string(self):
        client = TmuxClient()
        client.has_session = self._StubClient(alive_for_n_checks=99).has_session
        client.capture_pane = lambda name, lines=40: ""
        sleeps = []
        error = client.wait_for_alive_or_error("x", sleep_fn=sleeps.append)
        assert error == ""
        assert len(sleeps) == 4  # every check in the default schedule ran, none found it dead

    def test_dies_after_first_check_returns_text_captured_while_alive(self):
        # Alive for exactly the first check (so we get a chance to capture
        # its pane text), dead by the second.
        stub = self._StubClient(alive_for_n_checks=1, pane_text="No conversation found: x")
        client = TmuxClient()
        client.has_session = stub.has_session
        client.capture_pane = stub.capture_pane
        error = client.wait_for_alive_or_error("x", sleep_fn=lambda d: None)
        assert "No conversation found" in error

    def test_dies_with_no_captured_text_falls_back_to_generic_message(self):
        stub = self._StubClient(alive_for_n_checks=0, pane_text="")
        client = TmuxClient()
        client.has_session = stub.has_session
        client.capture_pane = stub.capture_pane
        error = client.wait_for_alive_or_error("x", sleep_fn=lambda d: None)
        assert error == "the session ended immediately"
