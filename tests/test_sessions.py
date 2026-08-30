"""Tests for state derivation and the tmux <-> Claude session mapping."""

from __future__ import annotations

from pathlib import Path

from conftest import SID1, SID2, SID3, FakeTmux, TranscriptBuilder, ts_ago

from cagents.claude_data import parse_session_file
from cagents.sessions import (
    SessionRegistry,
    SessionState,
    SessionView,
    check_state_invariant,
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

    def test_live_pending_tool_stale_is_working(self, claude_dir: Path, now: float):
        # Replicated live (haiku, 35s quiet foreground tool): an unanswered
        # tool call with no visible dialog is a tool still RUNNING. Guessing
        # "permission" here was the intermittent false "needs you".
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("run it").assistant_tool_use("t1", "Bash", {"command": "make big"})
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 120))
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.WORKING
        assert detail == "running Bash"

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

    def test_stale_scrollback_phrase_does_not_fake_a_live_spinner(self, claude_dir: Path, now: float):
        # Replicated live: a user literally typed "what is still running?"
        # earlier in the conversation. That phrase sitting in scrollback
        # must never be read as the live "· 1 shell still running"
        # spinner — only the actual last line of the pane (the real
        # footer) counts.
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("fix it").assistant_text("done", ts="2026-08-17T10:00:00.000Z")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        pane = (
            "❯ what is still running?\n\n"
            "⏺ Nothing — just Spring preloader daemons.\n\n"
            "─────────────────────\n"
            "❯ \n"
            "─────────────────────\n"
            "  Sonnet 5 | ctx: 37%\n"
            "  ⏵⏵ auto mode on · ← 1 agent"
        )
        state, _ = derive_state(parsed, _tracked(), live=True, pane_text=pane, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_shell_still_running_in_the_footer_outranks_monitoring(self, claude_dir: Path, now: float):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("kick off the migration").assistant_text(
            "Started.", ts="2026-08-17T10:00:00.000Z"
        )
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 300))
        pane = "  Sonnet 5 | ctx: 37%\n  ⏵⏵ auto mode on · 1 shell running · ← 1 agent"
        state, detail = derive_state(parsed, _tracked(), live=True, pane_text=pane, now=now)
        assert state == SessionState.SHELL_RUNNING
        assert "1 shell" in detail

        from cagents.sessions import ATTENTION_ORDER

        assert ATTENTION_ORDER[SessionState.SHELL_RUNNING] < ATTENTION_ORDER[SessionState.MONITORING]

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

    def test_freshly_tracked_no_transcript_yet_needs_input(self, now: float):
        # The `n` "new conversation" terminal tracks the session before the
        # user has actually typed `claude` in it — no transcript exists
        # yet. Within the grace window that must read as "waiting on you",
        # not "something went wrong."
        from datetime import datetime, timedelta, timezone

        added = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(minutes=2)
        tracked = _tracked()
        tracked.added_at = added.isoformat()
        state, detail = derive_state(None, tracked, live=True, now=now)
        assert state == SessionState.NEEDS_INPUT
        assert "claude" in detail

    def test_no_transcript_past_the_grace_window_is_stopped(self, now: float):
        from datetime import datetime, timedelta, timezone

        added = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(minutes=30)
        tracked = _tracked()
        tracked.added_at = added.isoformat()
        state, detail = derive_state(None, tracked, live=True, now=now)
        assert state == SessionState.STOPPED
        assert detail == "transcript missing"

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


class TestStateInvariant:
    """check_state_invariant: catches a state-derivation bug at the moment
    it happens (a reviewed session flipping back to NEEDS_REVIEW with no
    new transcript activity to justify it), instead of only when someone
    notices the wrong label live.

    Nothing -> WORKING is watched here — real bug, confirmed live: it
    false-positived the instant a user typed a message. derive_state
    checks the LIVE PANE before ever touching the transcript, and Claude
    Code updates the pane before the transcript file is flushed to disk,
    so `parsed.last_timestamp` lagging right after you type something is
    completely normal, not evidence of a bug — and this checker has no
    pane text to tell the two cases apart. See REQUIRES_NEW_ACTIVITY."""

    def _view(self, claude_dir: Path, now: float, state: SessionState, ts_offset: float) -> SessionView:
        b = TranscriptBuilder(SID1, "/proj/alpha").user("fix it").assistant_text(
            "done", ts="2026-08-17T10:00:00.000Z"
        )
        parsed = parse_session_file(b.write(claude_dir, mtime=now - ts_offset))
        return SessionView(session_id=SID1, tracked=_tracked(), parsed=parsed, state=state, live=True)

    def test_done_to_review_with_no_new_activity_is_a_violation(self, claude_dir: Path, now: float):
        view = self._view(claude_dir, now, SessionState.NEEDS_REVIEW, ts_offset=10)
        same_last_activity = view.parsed.last_timestamp
        violation = check_state_invariant(SessionState.DONE, same_last_activity, view)
        assert violation is not None
        assert "no new transcript activity" in violation

    def test_done_to_review_backed_by_new_activity_is_fine(self, claude_dir: Path, now: float):
        from datetime import timedelta

        view = self._view(claude_dir, now, SessionState.NEEDS_REVIEW, ts_offset=10)
        earlier = view.parsed.last_timestamp - timedelta(minutes=5)
        assert check_state_invariant(SessionState.DONE, earlier, view) is None

    def test_anything_to_working_is_never_flagged(self, claude_dir: Path, now: float):
        # The false positive this class of check must never regress to:
        # typing a message legitimately flips WORKING on via the live
        # pane, well before the transcript file catches up.
        view = self._view(claude_dir, now, SessionState.WORKING, ts_offset=10)
        same_last_activity = view.parsed.last_timestamp
        for previous in (SessionState.NEEDS_REVIEW, SessionState.DONE, SessionState.SNOOZED):
            assert check_state_invariant(previous, same_last_activity, view) is None

    def test_unwatched_pair_is_never_flagged(self, claude_dir: Path, now: float):
        view = self._view(claude_dir, now, SessionState.NEEDS_REVIEW, ts_offset=10)
        assert check_state_invariant(SessionState.WORKING, view.parsed.last_timestamp, view) is None

    def test_no_previous_state_is_not_flagged(self, claude_dir: Path, now: float):
        view = self._view(claude_dir, now, SessionState.WORKING, ts_offset=10)
        assert check_state_invariant(None, None, view) is None

    def test_unchanged_state_is_not_flagged(self, claude_dir: Path, now: float):
        view = self._view(claude_dir, now, SessionState.WORKING, ts_offset=10)
        assert check_state_invariant(SessionState.WORKING, view.parsed.last_timestamp, view) is None


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

    def test_two_sessions_same_dir_without_content_signal_refuses_to_guess(
        self, claude_dir: Path, now: float
    ):
        # Regression (found live, on a real shared apm_bundle checkout: 4
        # tmux panes and several tracked sessions all sharing the exact
        # same project_dir): tier 2 used to just hand the pane to whichever
        # tracked session had the newest transcript mtime, with zero check
        # that the pane was actually showing *that* conversation — a real
        # session's row ended up titled for one PR while the live pane
        # attached to it was an unrelated old conversation. Newest-mtime
        # is a guess, not a match; with no pane text to verify against, it
        # must refuse rather than wrongly attach.
        b1 = TranscriptBuilder(SID1, "/proj/alpha").user("old")
        p1 = parse_session_file(b1.write(claude_dir, mtime=now - 500))
        b2 = TranscriptBuilder(SID2, "/proj/alpha").user("new")
        p2 = parse_session_file(b2.write(claude_dir, mtime=now - 5))
        pairs = [(_tracked(SID1), p1), (_tracked(SID2), p2)]
        mapping = map_tmux_sessions(pairs, [_tmux(created=now - 600)])
        assert mapping == {}

    def test_two_sessions_same_dir_content_verification_picks_the_right_one(
        self, claude_dir: Path, now: float
    ):
        # Same setup as above, but now the pane's actual content is
        # available — must correctly identify SID1 as the real occupant
        # even though SID2 has the newer transcript mtime.
        b1 = TranscriptBuilder(SID1, "/proj/alpha").user("old")
        b1.assistant_text("The packets redesign is ready for review.")
        p1 = parse_session_file(b1.write(claude_dir, mtime=now - 500))
        b2 = TranscriptBuilder(SID2, "/proj/alpha").user("new")
        b2.assistant_text("Saved your token to the env file.")
        p2 = parse_session_file(b2.write(claude_dir, mtime=now - 5))
        pairs = [(_tracked(SID1), p1), (_tracked(SID2), p2)]
        tmux = _tmux(created=now - 600)
        pane = "…\n  The packets redesign\nis ready for review.\n❯ "
        mapping = map_tmux_sessions(pairs, [tmux], pane_text_fn=lambda t: pane)
        assert set(mapping) == {SID1}
        assert mapping[SID1].name == tmux.name

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

    def test_two_tmux_two_sessions_same_dir_without_content_refuses_both(
        self, claude_dir: Path, now: float
    ):
        # No way to tell which pane is which -> neither gets guessed.
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
        assert mapping == {}

    def test_two_tmux_two_sessions_same_dir_content_verification_pairs_correctly(
        self, claude_dir: Path, now: float
    ):
        # The exact live scenario this was found in: several tracked
        # sessions and several live panes all sharing one shared,
        # non-worktree checkout's directory. Each pane's real content
        # must route to its actual tracked session, never swapped.
        p1 = parse_session_file(
            TranscriptBuilder(SID1, "/proj/alpha").user("old")
            .assistant_text("The packets redesign is ready for review.")
            .write(claude_dir, mtime=now - 50)
        )
        p2 = parse_session_file(
            TranscriptBuilder(SID2, "/proj/alpha").user("new")
            .assistant_text("Saved your token to the env file.")
            .write(claude_dir, mtime=now - 5)
        )
        pairs = [(_tracked(SID1), p1), (_tracked(SID2), p2)]
        alpha = _tmux(name="alpha", created=now - 100)
        alpha2 = _tmux(name="alpha-2", created=now - 100)
        pane_text = {
            alpha.name: "…\n  Saved your token\nto the env file.\n❯ ",
            alpha2.name: "…\n  The packets redesign\nis ready for review.\n❯ ",
        }
        mapping = map_tmux_sessions(pairs, [alpha, alpha2], pane_text_fn=lambda t: pane_text[t.name])
        assert set(mapping) == {SID1, SID2}
        assert mapping[SID1].name == "alpha-2"  # the one actually showing the packets text
        assert mapping[SID2].name == "alpha"  # the one actually showing the token text


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

    def test_rank_stable_since_freezes_while_state_is_unchanged(
        self, claude_dir: Path, tmp_path: Path, now: float
    ):
        # The actual reported bug: two sessions that stay WORKING across
        # refreshes must not keep swapping places just because
        # last_activity ticks forward on every new token. rank_stable_since
        # should freeze at whenever each one *entered* WORKING, not track
        # last_activity at all.
        TranscriptBuilder(SID1, "/proj/alpha").user("go", ts=ts_ago(5)).write(
            claude_dir, mtime=now - 5
        )
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
        tmux = FakeTmux()
        tmux.sessions.append(_tmux(name="alpha", path="/proj/alpha", created=now - 60))
        registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)

        first = registry.refresh(now=now).by_id(SID1)
        assert first.state == SessionState.WORKING
        since_1 = first.rank_stable_since

        # last_activity ticks forward (new token/record) but the state
        # stays WORKING across several more refreshes.
        for offset in (1, 2, 3):
            view = registry.refresh(now=now + offset).by_id(SID1)
            assert view.state == SessionState.WORKING
            assert view.rank_stable_since == since_1  # frozen, unchanged

        # A genuine state change (turn finishes, well past the fresh-write
        # window) gets a fresh timestamp.
        from datetime import datetime, timedelta, timezone

        finished_ts = (
            datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(seconds=3)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        TranscriptBuilder(SID1, "/proj/alpha").user("go", ts=ts_ago(5)).assistant_text(
            "done", ts=finished_ts
        ).write(claude_dir, mtime=now + 3)
        later = registry.refresh(now=now + 60).by_id(SID1)
        assert later.state != SessionState.WORKING
        assert later.rank_stable_since == now + 60

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


class TestEnterWorktreeTranscriptMove:
    """Claude Code's EnterWorktree physically moves the transcript file into
    the worktree's encoded project dir (verified live on 2026-08-23). The
    registry must keep finding it — and must not pay a full projects scan on
    every refresh once it has."""

    def _registry(self, tmp_path):
        from cagents.sessions import SessionRegistry
        from cagents.store import Store

        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-23T10:00:00+00:00")
        claude_dir = tmp_path / "claude"
        return SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir), claude_dir

    def _write_transcript(self, claude_dir, encoded, sid):
        d = claude_dir / "projects" / encoded
        d.mkdir(parents=True)
        path = d / f"{sid}.jsonl"
        path.write_text(
            '{"type": "user", "sessionId": "%s", "cwd": "/proj/alpha-wt", '
            '"timestamp": "2026-08-23T10:00:00.000Z", '
            '"message": {"role": "user", "content": "hi"}}\n' % sid
        )
        return path

    def test_moved_transcript_found_and_cached(self, tmp_path):
        reg, claude_dir = self._registry(tmp_path)
        moved = self._write_transcript(claude_dir, "-proj-alpha--claude-worktrees-wt-a", SID1)
        tracked = reg.store.sessions[SID1]
        assert reg._find_session_file(tracked) == moved
        assert reg._file_cache[SID1] == moved
        # Cached: a second lookup must not rescan (poison discover_sessions).
        import cagents.sessions as sessions_mod
        orig = sessions_mod.discover_sessions
        sessions_mod.discover_sessions = lambda *a, **k: (_ for _ in ()).throw(AssertionError("rescanned"))
        try:
            assert reg._find_session_file(tracked) == moved
        finally:
            sessions_mod.discover_sessions = orig

    def test_second_move_invalidates_cache(self, tmp_path):
        reg, claude_dir = self._registry(tmp_path)
        first = self._write_transcript(claude_dir, "-proj-alpha--claude-worktrees-wt-a", SID1)
        tracked = reg.store.sessions[SID1]
        assert reg._find_session_file(tracked) == first
        # EnterWorktree again: transcript moves to wt-b's encoded dir.
        second = self._write_transcript(claude_dir, "-proj-alpha--claude-worktrees-wt-b", SID1)
        first.unlink()
        assert reg._find_session_file(tracked) == second


def test_refresh_polls_claude_agents_at_most_every_agent_poll_seconds(claude_dir: Path, tmp_path: Path, now: float):
    """`claude agents --json --all` boots the whole Claude CLI (~0.3s of a
    core each time) and refresh() ran it on every 2s tick — plus every
    explicit refresh_data() on top. Seen live as cagents burning 85% of a
    core while idle. It's a best-effort signal; poll it on its own, slower
    clock and reuse the answer in between."""
    from cagents.sessions import AGENT_POLL_SECONDS, SessionRegistry
    from cagents.store import Store

    TranscriptBuilder(SID1, "/proj/alpha").user("go").write(claude_dir)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        return "[]"

    registry = SessionRegistry(store, tmux=FakeTmuxEmpty(), claude_dir=claude_dir, agents_runner=runner)
    registry.refresh(now=now)
    registry.refresh(now=now + 2)
    registry.refresh(now=now + AGENT_POLL_SECONDS - 0.5)
    assert len(calls) == 1
    registry.refresh(now=now + AGENT_POLL_SECONDS)
    assert len(calls) == 2


def test_refresh_reparses_a_transcript_only_when_it_changed(claude_dir: Path, tmp_path: Path, now: float, monkeypatch):
    """Every tick re-read and re-parsed every tracked transcript (head +
    tail, up to ~48KB of JSON each) even though almost none of them change
    between ticks. Key the parse on (mtime, size) and reuse it."""
    from cagents import sessions as S
    from cagents.store import Store

    b = TranscriptBuilder(SID1, "/proj/alpha").user("go")
    b.write(claude_dir, mtime=now - 100)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    real = S.parse_session_file
    parses: list[Path] = []

    def counting(path, *a, **k):
        parses.append(path)
        return real(path, *a, **k)

    monkeypatch.setattr(S, "parse_session_file", counting)
    registry = S.SessionRegistry(store, tmux=FakeTmuxEmpty(), claude_dir=claude_dir, agents_runner=lambda a: "[]")
    registry.refresh(now=now)
    registry.refresh(now=now + 2)
    assert len(parses) == 1
    b.assistant_text("done").write(claude_dir, mtime=now - 50)  # the transcript grew
    registry.refresh(now=now + 4)
    assert len(parses) == 2
    assert registry.refresh(now=now + 6).views[0].parsed.last_record_role == "assistant"
    assert len(parses) == 2


class FakeTmuxEmpty:
    create_socket = "claude"

    def list_sessions(self):
        return []

    def capture_pane(self, *a, **k):
        return ""
