"""Tests for state derivation and the tmux <-> Claude session mapping."""

from __future__ import annotations

from pathlib import Path

from conftest import SID1, SID2, SID3, FakeTmux, TranscriptBuilder, ts_ago

from cagents.claude_data import parse_session_file
from cagents.sessions import (
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
    def test_missing_transcript_is_stopped(self):
        state, detail = derive_state(None, _tracked(), live=True)
        assert state == SessionState.STOPPED
        assert "missing" in detail

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

    def test_pane_in_ancestor_dir_matches_with_content(self, claude_dir: Path, now: float):
        # `claude` launched from $HOME, session working in a project subdir.
        # Ancestor matches must be content-verified: the pane really shows
        # this conversation (wrapping-insensitive).
        b = TranscriptBuilder(SID1, "/home/u/projects/deep").user("a")
        b.assistant_text("The quick brown fox refactor is complete now.")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 10))
        tmux = _tmux(name="home", path="/home/u", created=now - 60)
        pane = "…\n  The quick brown fox\nrefactor is complete now.\n❯ "
        mapping = map_tmux_sessions(
            [(_tracked(SID1, project="/home/u/projects/deep"), parsed)], [tmux],
            pane_text_fn=lambda t: pane,
        )
        assert mapping[SID1].name == "home"

    def test_ancestor_match_rejected_without_content(self, claude_dir: Path, now: float):
        # Regression (found live): unrelated tmux sessions in a parent dir
        # must NOT claim a stale transcript just because mtimes line up.
        b = TranscriptBuilder(SID1, "/home/u/projects/deep").user("a")
        b.assistant_text("Oculus Quest pricing estimate follows.")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 10))
        tmux = _tmux(name="home", path="/home/u", created=now - 60)
        mapping = map_tmux_sessions(
            [(_tracked(SID1, project="/home/u/projects/deep"), parsed)], [tmux],
            pane_text_fn=lambda t: "a completely different conversation about agents",
        )
        assert mapping == {}
        # ...and with no pane text available at all, same refusal:
        assert map_tmux_sessions(
            [(_tracked(SID1, project="/home/u/projects/deep"), parsed)], [tmux]
        ) == {}

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

        tmux = FakeTmux()
        tmux.sessions.append(_tmux(name="beta", path="/proj/beta", created=now - 30))
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
        tmux = FakeTmux()
        tmux.sessions.append(_tmux(name="alpha", path="/proj/alpha", created=now - 60))
        tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
        registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
        snap = registry.refresh(now=now)
        assert snap.views[0].state == SessionState.NEEDS_INPUT
