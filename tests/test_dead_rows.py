"""Rows with nothing behind them clean themselves up.

A tracked conversation whose transcript is gone (Claude Code prunes them
after cleanupPeriodDays) and that isn't running anywhere can never come
back: it sat in the list forever as "stopped · transcript missing". And an
`n` terminal nobody ever typed `claude` into turned into the same kind of
permanent stopped row once its 15-minute grace window ran out."""
import subprocess
from datetime import datetime, timedelta, timezone

from cagents.app import CagentsApp
from cagents.sessions import SessionState, SessionView, Snapshot
from cagents.store import Store
from conftest import FakeTmux


class KillTmux(FakeTmux):
    def __init__(self):
        super().__init__()
        self.killed = []

    def kill_session_group(self, name, socket=None):
        self.killed.append((name, socket))


def world(tmp_path, *views_spec):
    store = Store(tmp_path / "state.json")
    tmux = KillTmux()
    app = CagentsApp(store=store, tmux=tmux, claude_dir=tmp_path / "claude")
    added = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    views = []
    for sid, state, live, pane_command in views_spec:
        tracked = store.track(sid, str(tmp_path), added)
        views.append(SessionView(sid, tracked, None, state, live=live, missing=True,
                                 state_detail="transcript missing", tmux_name=f"t-{sid}",
                                 tmux_socket="cagents-sessions", pane_command=pane_command))
    app.snapshot = Snapshot(views=views)
    return app, store, tmux


def test_dead_row_with_no_transcript_and_no_process_is_archived(tmp_path):
    app, store, tmux = world(tmp_path, ("pruned", SessionState.STOPPED, False, ""))
    app._persist_lifecycle(app.snapshot)
    assert store.sessions["pruned"].archived
    assert Store.load(store.path).sessions["pruned"].archived  # durable, not just in memory
    assert tmux.killed == []


def test_unused_new_terminal_past_grace_is_closed_and_untracked(tmp_path):
    app, store, tmux = world(tmp_path, ("blank", SessionState.STOPPED, True, "zsh"))
    app._persist_lifecycle(app.snapshot)
    assert tmux.killed == [("t-blank", "cagents-sessions")]
    assert "blank" not in store.sessions
    assert "blank" not in Store.load(store.path).sessions


def test_live_agent_and_rows_still_in_grace_are_left_alone(tmp_path):
    app, store, tmux = world(
        tmp_path,
        # claude is running but hasn't written its first record yet
        ("starting", SessionState.STOPPED, True, "claude"),
        # within NEW_TERMINAL_GRACE_SECONDS derive_state says "waiting on you"
        ("fresh", SessionState.NEEDS_INPUT, True, "zsh"),
        ("fresh-dead", SessionState.NEEDS_INPUT, False, ""),
    )
    app._persist_lifecycle(app.snapshot)
    assert tmux.killed == []
    assert {sid: t.archived for sid, t in store.sessions.items()} == {
        "starting": False, "fresh": False, "fresh-dead": False}


def test_kill_session_group_kills_the_session_and_its_view_sessions(monkeypatch):
    # `n` terminals get grouped view sessions (name--term); killing only the
    # base would leave the shell alive inside the group.
    from cagents.tmuxctl import TmuxClient

    client = TmuxClient(sockets=("s",), create_socket="s")
    killed = []

    def fake_run(socket, *args, timeout=5.0):
        if args[0] == "list-sessions":
            out = "samir-5\tsamir-5\nsamir-5--term\tsamir-5\nother\tother\nsolo\t\n"
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        if args[0] == "kill-session":
            killed.append((socket, args[2]))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected tmux call {args}")

    monkeypatch.setattr(client, "_run", fake_run)
    client.kill_session_group("samir-5")
    assert killed == [("s", "=samir-5"), ("s", "=samir-5--term")]
    killed.clear()
    client.kill_session_group("solo", socket="s")
    assert killed == [("s", "=solo")]
