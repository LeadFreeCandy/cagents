"""Tests for state derivation and the tmux <-> Claude session mapping."""

from __future__ import annotations

from pathlib import Path

from conftest import SID1, SID2, SID3, TranscriptBuilder, ts_ago

from cagents.claude_data import parse_session_file
from cagents.sessions import (
    ATTENTION_ORDER,
    SessionRegistry,
    SessionState,
    derive_state,
    map_tmux_sessions,
)
from cagents.store import Store, TrackedSession
from cagents.tmuxctl import TmuxSession


def _tracked(sid: str = SID1, project: str = "/proj/alpha", reviewed_at: str = "") -> TrackedSession:
    return TrackedSession(
        session_id=sid,
        project_dir=project,
        added_at="2026-08-17T09:00:00+00:00",
        reviewed_at=reviewed_at,
    )


def _tmux(name: str = "alpha", path: str = "/proj/alpha", created: float = 0.0, sid: str = "") -> TmuxSession:
    return TmuxSession(
        name=name,
        created=created,
        activity=created,
        attached=False,
        pane_pid=123,
        pane_path=path,
        cagents_session_id=sid,
    )


class TestDeriveState:
    def test_missing_transcript_while_dead_is_stopped(self):
        state, detail = derive_state(None, _tracked(), live=False)
        assert state == SessionState.STOPPED
        assert "missing" in detail

    def test_missing_transcript_while_live_is_starting_not_dead(self):
        # A session that was just created/resumed: the tmux process is real
        # but Claude hasn't written its first transcript bytes yet. This
        # must never read as dead — previously it did (STOPPED, "transcript
        # missing"), which is what made brand-new sessions look broken.
        state, detail = derive_state(None, _tracked(), live=True)
        assert state == SessionState.WORKING
        assert "start" in detail

    def test_live_fresh_writes_is_working(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha").user("go", ts=ts_ago(2))
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 2))
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.WORKING

    def test_live_pending_tool_stale_is_needs_input(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("run it").assistant_tool_use("t1", "Bash", {"command": "rm -rf build"})
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 120))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_INPUT
        assert detail == "permission: Bash"

    def test_live_monitor_started_and_idle_is_monitoring_not_needs_input(
        self, claude_dir: Path, now: float
    ):
        # Real shape, verified against an actual transcript: a Monitor
        # tool_use gets an immediate, synchronous tool_result ("Monitor
        # started...") — so it's resolved, not pending — and the turn
        # simply ends there. Without last_resolved_tool_name, that reads
        # as a generic "at the prompt" (NEEDS_INPUT), which is the exact
        # false "needs you" this state exists to fix.
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("watch CI and tell me").assistant_tool_use(
            "t1", "Monitor", {"command": "while true; do ...; done"}
        ).tool_result("t1")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.MONITORING
        assert detail == "monitor running"

    def test_live_backgrounded_bash_and_idle_is_background(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("kick off the build").assistant_tool_use(
            "t1", "Bash", {"command": "npm run build", "run_in_background": True}
        ).tool_result("t1")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.BACKGROUND
        assert detail == "background running"

    def test_live_pending_background_agents_and_idle_is_background(
        self, claude_dir: Path, now: float
    ):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("go wide").assistant_tool_use("t1", "Task", {"prompt": "do the thing"}).tool_result(
            "t1"
        ).raw(
            {"type": "system", "subtype": "turn_duration", "durationMs": 100,
             "pendingBackgroundAgentCount": 2, "isSidechain": False}
        )
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.BACKGROUND
        assert detail == "background running"

    def test_regular_bash_result_and_idle_is_still_needs_input(self, claude_dir: Path, now: float):
        # Same shape as the monitor/background cases (resolved tool_use,
        # then nothing) but an ordinary foreground Bash call — must still
        # be treated as genuinely idle-at-the-prompt.
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("what's in this dir").assistant_tool_use(
            "t1", "Bash", {"command": "ls"}
        ).tool_result("t1")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_INPUT
        assert detail == "at the prompt"

    def test_monitoring_beats_background_when_both_signals_present(
        self, claude_dir: Path, now: float
    ):
        # A Monitor was the *last* resolved tool call, but background
        # agents are also pending — Monitor is checked first (higher
        # priority signal), per the requested ranking.
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("watch it").assistant_tool_use(
            "t1", "Monitor", {"command": "..."}
        ).tool_result("t1").raw(
            {"type": "system", "subtype": "turn_duration", "durationMs": 100,
             "pendingBackgroundAgentCount": 1, "isSidechain": False}
        )
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.MONITORING

    def test_monitoring_and_background_rank_below_working_above_stopped(self):
        order = ATTENTION_ORDER
        assert (
            order[SessionState.WORKING]
            < order[SessionState.MONITORING]
            < order[SessionState.BACKGROUND]
            < order[SessionState.STOPPED]
        )

    def test_live_pane_prompt_wins_even_with_fresh_writes(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("run it").assistant_tool_use("t1", "Bash", {"command": "ls"})
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 1))
        pane = "Bash command\n  ls\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
        state, _ = derive_state(parsed, _tracked(), live=True, pane_text=pane, now=now)
        assert state == SessionState.NEEDS_INPUT

    def test_live_pane_working_marker_overrides_stale_pending_tool(self, claude_dir: Path, now: float):
        # A long-running quiet tool: no writes for minutes, but the pane
        # shows the spinner — that's WORKING, not a permission prompt.
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("run it").assistant_tool_use("t1", "Bash", {"command": "sleep 600"})
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        pane = "✻ Simmering… (esc to interrupt)"
        state, detail = derive_state(parsed, _tracked(), live=True, pane_text=pane, now=now)
        assert state == SessionState.WORKING
        assert detail == "running Bash"

    def test_live_finished_turn_is_needs_review(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("fix it").assistant_text("done", ts="2026-08-17T10:00:00.000Z")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_reviewed_after_last_activity_is_done(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("fix it").assistant_text("done", ts="2026-08-17T10:00:00.000Z")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        tracked = _tracked(reviewed_at="2026-08-17T11:00:00+00:00")
        state, _ = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.DONE

    def test_review_goes_stale_when_claude_does_more_work(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("fix it").assistant_text("done", ts="2026-08-17T12:00:00.000Z")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        tracked = _tracked(reviewed_at="2026-08-17T11:00:00+00:00")  # before last activity
        state, _ = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_dead_mid_turn_is_stopped(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("run it").assistant_tool_use("t1", "Bash", {"command": "ls"})
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, detail = derive_state(parsed, _tracked(), live=False, now=now)
        assert state == SessionState.STOPPED
        assert detail == "ended mid-turn"

    def test_dead_finished_turn_is_needs_review(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("fix it").assistant_text("done")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        state, _ = derive_state(parsed, _tracked(), live=False, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_resume_touch_is_not_working(self, claude_dir: Path, now: float):
        # THE bug: entering a conversation and leaving it touches the file
        # mtime without appending records. Fresh mtime + old conversation
        # must not read as "working".
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("fix it").assistant_text("done", ts="2026-08-17T10:00:00.000Z")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 1))  # just touched
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_empty_live_session_is_at_the_prompt(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha").ai_title("fresh")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 1))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_INPUT
        assert detail == "at the prompt"

    def test_live_at_prompt_no_reply_yet(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha").user("hello?")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 60))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_INPUT
        assert detail == "at the prompt"


class TestTmuxMapping:
    def test_env_var_match_wins(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha").user("a")
        parsed = parse_session_file(b.write(claude_dir, mtime=now))
        tmux = _tmux(name="other-dir", path="/somewhere/else", sid=SID1)
        mapping = map_tmux_sessions([(_tracked(), parsed)], [tmux])
        assert mapping[SID1].name == "other-dir"

    def test_cwd_match(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha").user("a")
        parsed = parse_session_file(b.write(claude_dir, mtime=now))
        mapping = map_tmux_sessions([(_tracked(), parsed)], [_tmux(created=now - 60)])
        assert mapping[SID1].name == "alpha"

    def test_old_transcript_does_not_claim_new_tmux(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha").user("a")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 7200))
        # tmux session created an hour after the transcript last changed
        mapping = map_tmux_sessions([(_tracked(), parsed)], [_tmux(created=now - 3600)])
        assert mapping == {}

    def test_two_sessions_same_dir_newest_claims_it(self, claude_dir: Path, now: float):
        b1 = TranscriptBuilder(SID1, "/proj/alpha").user("old")
        p1 = parse_session_file(b1.write(claude_dir, mtime=now - 500))
        b2 = TranscriptBuilder(SID2, "/proj/alpha").user("new")
        p2 = parse_session_file(b2.write(claude_dir, mtime=now - 5))
        pairs = [(_tracked(SID1), p1), (_tracked(SID2), p2)]
        mapping = map_tmux_sessions(pairs, [_tmux(created=now - 600)])
        assert set(mapping) == {SID2}

    def test_pane_in_ancestor_dir_matches(self, claude_dir: Path, now: float):
        # `claude` launched from $HOME, session working in a project subdir.
        b = TranscriptBuilder(SID1, "/home/u/projects/deep").user("a")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 10))
        tmux = _tmux(name="home", path="/home/u", created=now - 60)
        mapping = map_tmux_sessions(
            [(_tracked(SID1, project="/home/u/projects/deep"), parsed)], [tmux]
        )
        assert mapping[SID1].name == "home"

    def test_exact_match_beats_ancestor_match(self, claude_dir: Path, now: float):
        p1 = parse_session_file(
            TranscriptBuilder(SID1, "/home/u/projects/deep").user("a").write(claude_dir, mtime=now - 5)
        )
        p2 = parse_session_file(
            TranscriptBuilder(SID2, "/home/u").user("b").write(claude_dir, mtime=now - 300)
        )
        pairs = [
            (_tracked(SID1, project="/home/u/projects/deep"), p1),
            (_tracked(SID2, project="/home/u"), p2),
        ]
        tmux = _tmux(name="home", path="/home/u", created=now - 600)
        mapping = map_tmux_sessions(pairs, [tmux])
        # SID2 matches /home/u exactly, so it wins despite being older.
        assert set(mapping) == {SID2}

    def test_two_tmux_two_sessions_same_dir(self, claude_dir: Path, now: float):
        p1 = parse_session_file(
            TranscriptBuilder(SID1, "/proj/alpha").user("old").write(claude_dir, mtime=now - 50)
        )
        p2 = parse_session_file(
            TranscriptBuilder(SID2, "/proj/alpha").user("new").write(claude_dir, mtime=now - 5)
        )
        pairs = [(_tracked(SID1), p1), (_tracked(SID2), p2)]
        tmuxes = [
            _tmux(name="alpha", created=now - 100),
            _tmux(name="alpha-2", created=now - 100),
        ]
        mapping = map_tmux_sessions(pairs, tmuxes)
        assert set(mapping) == {SID1, SID2}
        assert mapping[SID1].name != mapping[SID2].name


class FakeTmux:
    """Test double for TmuxClient."""

    def __init__(self, sessions: list[TmuxSession] | None = None, panes: dict[str, str] | None = None):
        self.sessions = sessions or []
        self.panes = panes or {}

    def list_sessions(self):
        return self.sessions

    def capture_pane(self, name: str, lines: int = 40) -> str:
        return self.panes.get(name, "")


class TestRegistry:
    def test_refresh_builds_views(self, claude_dir: Path, tmp_path: Path, now: float):
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
        store.track(SID2, "/proj/beta", "2026-08-17T09:00:00+00:00")

        TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha work").user("go").assistant_text(
            "done"
        ).write(claude_dir, mtime=now - 400)
        TranscriptBuilder(SID2, "/proj/beta").ai_title("Beta work").user(
            "go", ts=ts_ago(1)
        ).write(claude_dir, mtime=now - 1)

        tmux = FakeTmux([_tmux(name="beta", path="/proj/beta", created=now - 30)])
        registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
        snap = registry.refresh(now=now)

        assert len(snap.views) == 2
        alpha = snap.by_id(SID1)
        beta = snap.by_id(SID2)
        assert alpha.state == SessionState.NEEDS_REVIEW
        assert alpha.live is False
        assert beta.state == SessionState.WORKING
        assert beta.live is True
        assert beta.tmux_name == "beta"
        counts = snap.counts()
        assert counts[SessionState.WORKING] == 1
        assert counts[SessionState.NEEDS_REVIEW] == 1

    def test_refresh_handles_missing_transcript(self, claude_dir: Path, tmp_path: Path, now: float):
        store = Store.load(tmp_path / "state.json")
        store.track(SID3, "/proj/ghost", "2026-08-17T09:00:00+00:00")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        snap = registry.refresh(now=now)
        assert snap.views[0].missing is True
        assert snap.views[0].state == SessionState.STOPPED
        assert snap.views[0].title  # still renders something

    def test_finds_file_when_encoding_mismatches(self, claude_dir: Path, tmp_path: Path, now: float):
        # Session written under a project dir that doesn't match the naive
        # encoding of the tracked project_dir (e.g. started in a subdir).
        TranscriptBuilder(SID1, "/proj/alpha/sub").user("hi").write(claude_dir, mtime=now)
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        snap = registry.refresh(now=now)
        assert snap.views[0].missing is False
        assert snap.views[0].parsed.cwd == "/proj/alpha/sub"

    def test_discover_untracked_excludes_tracked(self, claude_dir: Path, tmp_path: Path, now: float):
        TranscriptBuilder(SID1, "/proj/alpha").user("a").write(claude_dir, mtime=now - 10)
        TranscriptBuilder(SID2, "/proj/beta").user("b").write(claude_dir, mtime=now - 5)
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        untracked = registry.discover_untracked()
        assert [s.session_id for s in untracked] == [SID2]

    def test_pane_prompt_forces_needs_input(self, claude_dir: Path, tmp_path: Path, now: float):
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
        TranscriptBuilder(SID1, "/proj/alpha").user("go").assistant_tool_use(
            "t1", "Bash", {"command": "make deploy"}
        ).write(claude_dir, mtime=now - 3)
        tmux = FakeTmux(
            [_tmux(name="alpha", path="/proj/alpha", created=now - 60)],
            panes={"alpha": "Do you want to proceed?\n❯ 1. Yes"},
        )
        registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
        snap = registry.refresh(now=now)
        assert snap.views[0].state == SessionState.NEEDS_INPUT
