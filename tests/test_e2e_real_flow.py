"""A genuinely real end-to-end run of the software — no FakeTmux, no mocked
Sidecar. Real outer tmux server hosting the sidecar rail, real tmux
sessions standing in for Claude, the actual CagentsApp driven through a
Textual pilot. Everything is asserted by literally reading back real tmux
pane content, not by inspecting cagents' own bookkeeping.

The one thing not real is the `claude` binary itself, replaced by a small
script that prints a marker and stays alive — swapping in the *real*
`claude` CLI is covered separately (and expensively/riskily) in
test_real_tmux_flow.py; what this file verifies is cagents' own mechanics:
create a session -> it's actually reachable; resume a session -> it's
actually reachable; and — the specific ask driving this file — that the
sidecar's passive preview and an explicit `enter` attach are the *same*
underlying tmux target, so there is no separate "preview renderer" for
them to disagree on. If they attach to the same pty, the formatting is
provably identical, because it's the same terminal being looked at twice.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import TranscriptBuilder

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry
from cagents.sidecar import Sidecar
from cagents.store import Store
from cagents.tmuxctl import TmuxClient

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def _run_outer(socket: str, *args: str) -> str:
    proc = subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"tmux {args[0]} failed")
    return proc.stdout


@pytest.fixture
def claude_tmux():
    """A real TmuxClient on its own throwaway socket — stands in for the
    user's real `claude`-socket server, isolated from anything real."""
    socket = f"cagents-e2e-claude-{uuid.uuid4().hex[:8]}"
    client = TmuxClient(socket=socket)
    yield client
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


@pytest.fixture
def sidecar():
    """A real Sidecar backed by a real, throwaway *outer* tmux server —
    the container the rail and the attached session's pane would really
    live in. `own_pane` is the real pane id of a genuine tmux pane."""
    socket = f"cagents-e2e-outer-{uuid.uuid4().hex[:8]}"
    _run_outer(socket, "new-session", "-d", "-s", "host", "-x", "220", "-y", "50", "cat")
    own_pane = _run_outer(socket, "list-panes", "-t", "host", "-F", "#{pane_id}").strip()

    def runner(args: list[str]) -> str:
        return _run_outer(socket, *args)

    sc = Sidecar(runner=runner, own_pane=own_pane)
    sc._socket = socket  # stashed for the test to read the pane back
    yield sc
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def _capture_sidecar_pane(sc: Sidecar) -> str:
    return subprocess.run(
        ["tmux", "-L", sc._socket, "capture-pane", "-p", "-t", sc.pane_id],
        capture_output=True, text=True, timeout=10,
    ).stdout


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    """Stands in for the real CLI: prints a unique, greppable marker so
    tests can prove the sidecar pane is genuinely showing *this* live
    process's real pty output, then stays alive."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'echo "FAKE_CLAUDE_LIVE $1 $2 $3"\n'
        "sleep 60\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def app_env(tmp_path: Path, claude_tmux: TmuxClient, sidecar: Sidecar, fake_claude: str):
    claude_dir = tmp_path / "claude-home"
    claude_dir.mkdir()
    store = Store.load(tmp_path / "state.json")
    registry = SessionRegistry(store, tmux=claude_tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=claude_tmux, claude_dir=claude_dir, sidecar=sidecar)
    app._claude_bin = lambda: fake_claude  # skip PATH lookup, use our stand-in
    return app, store, claude_tmux, sidecar


class TestRealEndToEndFlow:
    async def test_new_session_is_actually_accessible(self, app_env):
        app, store, claude_tmux, sidecar = app_env
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app._new_session_chosen(("/tmp", "e2e new session"))
            await pilot.pause()

            (session_id,) = store.sessions.keys()
            assert claude_tmux.has_session(app._recently_started[session_id]), (
                "a session cagents just created must actually be a live, reachable tmux session"
            )
            pane_text = _capture_sidecar_pane(sidecar)
            assert "FAKE_CLAUDE_LIVE" in pane_text, (
                "attaching to a brand-new session should show its real live output in the "
                "sidecar pane, not nothing / an error"
            )

    async def test_existing_tracked_session_resume_is_functional(self, app_env):
        app, store, claude_tmux, sidecar = app_env
        # A genuinely *existing* session: real prior transcript on disk,
        # tracked, but nothing currently running for it — not a bare
        # bookkeeping entry with zero history (that's correctly refused).
        session_id = str(uuid.uuid4())
        TranscriptBuilder(session_id, "/tmp").user("hello").assistant_text("hi there").write(
            app.claude_dir
        )
        store.track(session_id, "/tmp", datetime.now(timezone.utc).isoformat())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            view = app.snapshot.by_id(session_id)
            assert view is not None and not view.live  # nothing running for it yet

            app.selected_session_id = session_id
            app.action_attach()  # dead -> should resume it for real
            await pilot.pause()

            name = app._recently_started.get(session_id)
            assert name and claude_tmux.has_session(name), (
                "resuming an existing tracked session must result in an actually-live, "
                "reachable tmux session — not a silent no-op"
            )
            pane_text = _capture_sidecar_pane(sidecar)
            assert "FAKE_CLAUDE_LIVE" in pane_text

    async def test_hover_preview_and_enter_attach_hit_the_identical_pty(self, app_env):
        """The specific ask: the passive preview (just selecting/hovering a
        session) must render exactly like pressing enter — because both
        must be the same real tmux attach to the same real session, not
        two different rendering paths that could ever disagree."""
        app, store, claude_tmux, sidecar = app_env
        session_id = str(uuid.uuid4())
        store.track(session_id, "/tmp", datetime.now(timezone.utc).isoformat())
        name = claude_tmux.new_claude_session(
            "/tmp", ["--resume", session_id], session_id=session_id, claude_bin=app._claude_bin(),
        )
        assert claude_tmux.wait_for_alive_or_error(name) == ""  # sanity: it's really alive

        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.selected_session_id = session_id
            app._update_preview()  # what merely highlighting the row triggers
            await pilot.pause(0.3)

            preview_pane_text = _capture_sidecar_pane(sidecar)
            assert "FAKE_CLAUDE_LIVE" in preview_pane_text, (
                "hovering/selecting a live session must show its REAL terminal output in "
                "the sidecar pane (a real tmux attach), not a custom-rendered summary"
            )
            preview_command = subprocess.run(
                ["tmux", "-L", sidecar._socket, "display-message", "-p", "-t", sidecar.pane_id,
                 "#{pane_start_command}"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            assert " -r " in preview_command, "the passive preview must attach read-only"
            assert name in preview_command

            app.action_attach()  # promote: same target, now interactive
            await pilot.pause(0.3)

            attach_pane_text = _capture_sidecar_pane(sidecar)
            attach_command = subprocess.run(
                ["tmux", "-L", sidecar._socket, "display-message", "-p", "-t", sidecar.pane_id,
                 "#{pane_start_command}"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            assert name in attach_command
            assert " -r " not in attach_command, "enter must promote to a real (writable) attach"

            # Same underlying session both times -> literally the same pty,
            # so the rendered content cannot differ between preview and enter.
            assert "FAKE_CLAUDE_LIVE" in attach_pane_text
