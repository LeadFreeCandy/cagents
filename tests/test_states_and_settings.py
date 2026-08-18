"""Tests for state derivation hardening, the new derived states
(monitoring / background), waiting-on-external, settings, and
notification gating."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import SID1, SID2, FakeTmux, TranscriptBuilder, ts_ago

from cagents.app import CagentsApp
from cagents.claude_data import parse_session_file
from cagents.gitops import PRStatus
from cagents.notifier import read_select_request
from cagents.sessions import SessionRegistry, SessionState, derive_state
from cagents.store import SETTINGS_DEFAULTS, Store, TrackedSession
from cagents.tmuxctl import TmuxSession, extract_prompt_question, pane_shows_prompt


# ------------------------------------------------------------- settings ---


class TestSettings:
    def test_defaults(self, tmp_path: Path):
        store = Store.load(tmp_path / "state.json")
        assert store.get_setting("sidebar") is True
        assert store.get_setting("notifications") is False
        assert store.get_setting("capture_left") is True
        assert store.get_setting("desktop_notifications") is False

    def test_roundtrip_and_unknown_keys(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = Store.load(path)
        store.set_setting("notifications", True)
        store.set_setting("bogus", True)  # silently ignored
        reloaded = Store.load(path)
        assert reloaded.get_setting("notifications") is True
        assert "bogus" not in reloaded.settings

    def test_meta_matches_defaults(self):
        from cagents.modals import SETTINGS_META

        toggles = {k for k, _, _ in SETTINGS_META}
        # state_order lives in the Priority tab, not the toggle list
        assert toggles == set(SETTINGS_DEFAULTS) - {"state_order"}

    def test_reset_wipes_bookkeeping_only(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = Store.load(path)
        store.track(SID1, "/proj/a", "2026-08-18T09:00:00+00:00")
        store.set_setting("notifications", True)
        store.reset()
        reloaded = Store.load(path)
        assert reloaded.sessions == {} and reloaded.settings == {}


class TestWaitingStore:
    def test_waiting_roundtrip(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = Store.load(path)
        store.track(SID1, "/proj/a", "2026-08-18T09:00:00+00:00")
        store.set_waiting(SID1, "2026-08-18T10:00:00+00:00", "https://x/pull/1")
        got = Store.load(path).sessions[SID1]
        assert got.waiting_since and got.waiting_pr == "https://x/pull/1"
        store.clear_waiting(SID1, external_update="merged")
        got = Store.load(path).sessions[SID1]
        assert got.waiting_since == "" and got.external_update == "merged"


# ------------------------------------------------ prompt / state hardening ---


def test_prompt_detection_requires_dialog_signature():
    assert pane_shows_prompt("Do you want me to also refactor the parser?") is False
    assert pane_shows_prompt("Steps:\n 1. build\n 2. deploy") is False
    assert pane_shows_prompt("Do you want to proceed?\n❯ 1. Yes\n  2. No") is True


def test_extract_prompt_question():
    pane = "│ Do you want to proceed? │\n│ ❯ 1. Yes │"
    assert "Do you want to proceed?" in extract_prompt_question(pane)
    assert extract_prompt_question("just working…") == ""


def _tracked(**kw) -> TrackedSession:
    return TrackedSession(SID1, "/proj/a", "2026-08-18T09:00:00+00:00", **kw)


class TestNewStates:
    def _parse(self, claude_dir, builder):
        return parse_session_file(builder.write(claude_dir, mtime=time.time() - 300))

    def test_monitor_ack_yields_monitoring(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("watch the deploy")
        b.assistant_tool_use("t1", "Monitor", {"description": "watch deploy"})
        b.raw_tool_result("t1", "Monitor started (task abc123, timeout 600000ms). "
                                "You will be notified on each event.")
        b.assistant_text("Watching the deploy now.", ts="2026-08-18T10:00:10.000Z")
        parsed = self._parse(claude_dir, b)
        assert parsed.monitor_active is True
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.MONITORING
        assert "monitor" in detail.lower()

    def test_background_ack_yields_background(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("run the long build")
        b.assistant_tool_use("t1", "Bash", {"command": "make", "run_in_background": True})
        b.raw_tool_result("t1", "Command running in background with ID: bash_1. "
                                "You will be notified when it completes.")
        b.assistant_text("Build started in the background.", ts="2026-08-18T10:00:10.000Z")
        parsed = self._parse(claude_dir, b)
        assert parsed.background_active is True
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.BACKGROUND

    def test_new_human_message_resets_watches(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("watch it")
        b.assistant_tool_use("t1", "Monitor", {"description": "x"})
        b.raw_tool_result("t1", "Monitor started (task z, timeout 1ms).")
        b.user("actually, do something else", ts="2026-08-18T10:05:00.000Z")
        b.assistant_text("Done with the new thing.", ts="2026-08-18T10:06:00.000Z")
        parsed = self._parse(claude_dir, b)
        assert parsed.monitor_active is False
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_monitoring_ranks_between_review_and_working(self):
        from cagents.sessions import ATTENTION_ORDER

        order = ATTENTION_ORDER
        assert (
            order[SessionState.NEEDS_REVIEW]
            < order[SessionState.MONITORING]
            < order[SessionState.BACKGROUND]
            < order[SessionState.WORKING]
        )

    def test_waiting_external_derivation(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("ship it").assistant_text("PR is up.", ts="2026-08-18T10:00:00.000Z")
        parsed = self._parse(claude_dir, b)
        tracked = _tracked(waiting_since="2026-08-18T11:00:00+00:00", waiting_pr="url")
        state, detail = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.WAITING_EXTERNAL
        # new local activity after the mark -> back to needs review
        tracked = _tracked(waiting_since="2026-08-18T09:00:00+00:00", waiting_pr="url")
        state, _ = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_external_update_shows_in_detail(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("ship it").assistant_text("PR is up.", ts="2026-08-18T10:00:00.000Z")
        parsed = self._parse(claude_dir, b)
        state, detail = derive_state(
            parsed, _tracked(external_update="github comments"), live=False, now=now
        )
        assert state == SessionState.NEEDS_REVIEW
        assert detail == "github comments"


class TestDebounce:
    def _registry(self, claude_dir, tmp_path, now, pane):
        TranscriptBuilder(SID1, "/proj/alpha").user("go").assistant_tool_use(
            "t1", "Bash", {"command": "ls"}
        ).write(claude_dir, mtime=now - 1)
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
        tmux = FakeTmux()
        tmux.sessions.append(
            TmuxSession(name="alpha", created=now - 60, activity=now, attached=False,
                        pane_pid=1, pane_path="/proj/alpha", socket="claude")
        )
        tmux.panes["alpha"] = pane
        return SessionRegistry(store, tmux=tmux, claude_dir=claude_dir), tmux

    def test_working_to_input_held_one_cycle(self, claude_dir, tmp_path, now):
        registry, tmux = self._registry(claude_dir, tmp_path, now, "✻ Running… (esc to interrupt)")
        assert registry.refresh(now=now).views[0].state == SessionState.WORKING
        tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
        assert registry.refresh(now=now + 2).views[0].state == SessionState.WORKING  # held
        confirmed = registry.refresh(now=now + 4).views[0]
        assert confirmed.state == SessionState.NEEDS_INPUT
        assert confirmed.needs_line == "Do you want to proceed?"

    def test_first_observation_trusted_immediately(self, claude_dir, tmp_path, now):
        registry, _ = self._registry(claude_dir, tmp_path, now, "Do you want to proceed?\n❯ 1. Yes")
        assert registry.refresh(now=now).views[0].state == SessionState.NEEDS_INPUT


# ------------------------------------------------------------- app-level ---


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Fix auth").user("go").assistant_text(
        "All fixed."
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


async def test_waiting_key_uses_recorded_pr(world, claude_dir, now):
    app, store, tmux = world
    sid = "55555555-5555-5555-5555-555555555555"
    TranscriptBuilder(sid, "/proj/pr").ai_title("PR work").user("go").raw(
        {"type": "pr-link", "sessionId": sid, "prNumber": 9,
         "prUrl": "https://github.com/o/r/pull/9"}
    ).assistant_text("PR opened.").write(claude_dir, mtime=now - 600)
    store.track(sid, "/proj/pr", "2026-08-18T09:00:00+00:00")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from conftest import select_session

        select_session(app, sid)
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause(0.2)
        tracked = store.sessions[sid]
        assert tracked.waiting_pr == "https://github.com/o/r/pull/9"
        assert app.snapshot.by_id(sid).state == SessionState.WAITING_EXTERNAL
        # toggle off
        await pilot.press("w")
        await pilot.pause(0.2)
        assert store.sessions[sid].waiting_since == ""


async def test_waiting_key_prompts_when_no_pr_found(world):
    app, store, tmux = world
    app.gh_runner = lambda args, cwd=None: (_ for _ in ()).throw(RuntimeError("no PR"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from conftest import select_session

        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause(0.3)  # gh lookup worker fails -> prompt
        from cagents.modals import InputModal

        assert isinstance(app.screen, InputModal)
        await pilot.press(*"https://github.com/o/r/pull/42")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert store.sessions[SID1].waiting_pr == "https://github.com/o/r/pull/42"


async def test_pr_poll_reopens_on_comments_and_closes_on_merge(world):
    app, store, tmux = world
    store.set_waiting(SID1, "2026-08-18T10:00:00+00:00", "https://x/pull/1")

    # New comments after the waiting mark -> re-alert as needs review
    app.gh_runner = lambda args, cwd=None: (
        '{"state": "OPEN", "mergedAt": null,'
        ' "comments": [{"createdAt": "2026-08-18T11:00:00+00:00"}], "reviews": []}'
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_waiting_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[SID1]
        assert tracked.waiting_since == ""
        assert tracked.external_update == "github comments"
        view = app.snapshot.by_id(SID1)
        assert view.state == SessionState.NEEDS_REVIEW
        assert view.state_detail == "github comments"

        # Park again; now the PR merges -> auto done
        store.set_waiting(SID1, "2026-08-18T12:00:00+00:00", "https://x/pull/1")
        app.gh_runner = lambda args, cwd=None: (
            '{"state": "MERGED", "mergedAt": "2026-08-18T13:00:00+00:00",'
            ' "comments": [], "reviews": []}'
        )
        app._poll_waiting_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[SID1]
        assert tracked.reviewed_at != ""
        assert tracked.external_update == "merged"
        view = app.snapshot.by_id(SID1)
        assert view.state == SessionState.DONE
        assert view.state_detail == "merged"


async def test_notifications_gated_by_setting(world):
    app, store, _ = world
    captured = []
    import textual.app as textual_app

    original = textual_app.App.notify

    def spy(self, message, **kwargs):
        captured.append((kwargs.get("severity", "information"), str(message)))

    textual_app.App.notify = spy
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            captured.clear()
            app.notify("routine info")
            app.notify("careful", severity="warning")
            app.notify("boom", severity="error")
            assert [s for s, _ in captured] == ["warning", "error"]
            store.set_setting("notifications", True)
            app.notify("routine info")
            assert captured[-1] == ("information", "routine info")
    finally:
        textual_app.App.notify = original


async def test_desktop_notification_on_transition(world, monkeypatch):
    app, store, tmux = world
    store.set_setting("desktop_notifications", True)
    fired = []
    monkeypatch.setattr(
        "cagents.app.notify_desktop",
        lambda title, msg, sid, state_dir, **kw: fired.append((title, sid)),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert fired == []  # startup snapshot never notifies
        app._prev_states[SID1] = SessionState.WORKING
        app.apply_snapshot(app.registry.refresh())
        await pilot.pause(0.3)
        assert fired and fired[0][1] == SID1


async def test_click_select_request(world):
    app, store, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("3")  # kanban
        await pilot.pause()
        (store.path.parent / "select-request").write_text(SID1 + "\n")
        app.apply_snapshot(app.registry.refresh())
        await pilot.pause()
        assert app.active_view_id == "queue"
        assert app.selected_session_id == SID1
        assert read_select_request(store.path.parent) is None  # consumed


async def test_settings_modal_toggles_and_persists(world):
    app, store, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        from cagents.modals import SettingsModal

        assert isinstance(app.screen, SettingsModal)
        await pilot.press("enter")  # first row: sidebar -> off
        await pilot.pause(0.1)
        assert store.get_setting("sidebar") is False
        assert Store.load(store.path).get_setting("sidebar") is False
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert store.get_setting("sidebar") is True
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsModal)


class TestActiveElsewhere:
    def test_fresh_transcript_without_tmux_is_working(self, claude_dir, now):
        # Hosted by cmux or a bare terminal: no tmux session visible, but the
        # transcript is being written right now.
        b = TranscriptBuilder(SID1, "/proj/a").user("go", ts=ts_ago(3))
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 3))
        state, detail = derive_state(parsed, _tracked(), live=False, now=now)
        assert state == SessionState.WORKING
        assert "outside" in detail


async def test_enter_refuses_duplicate_cli_for_active_elsewhere(world, claude_dir, now):
    app, store, tmux = world
    sid = "77777777-7777-7777-7777-777777777777"
    TranscriptBuilder(sid, "/tmp").user("busy", ts=ts_ago(2)).write(claude_dir, mtime=now - 2)
    store.track(sid, "/tmp", "2026-08-18T09:00:00+00:00")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from conftest import select_session

        select_session(app, sid)
        await pilot.pause()
        assert app.snapshot.by_id(sid).state == SessionState.WORKING
        app.action_attach()
        await pilot.pause()
        assert tmux.created == []  # no duplicate claude was spawned


class TestStatePriority:
    def test_default_rank_map(self):
        from cagents.sessions import ATTENTION_ORDER, attention_rank_map

        assert attention_rank_map(None) == dict(ATTENTION_ORDER)
        assert attention_rank_map("garbage") == dict(ATTENTION_ORDER)

    def test_custom_order_wins_and_is_unbrickable(self):
        from cagents.sessions import attention_rank_map

        rank = attention_rank_map(["working", "needs input", "not-a-state"])
        assert rank[SessionState.WORKING] == 0
        assert rank[SessionState.NEEDS_INPUT] == 1
        # everything else appended in default order — nothing missing
        assert len(rank) == len(SessionState)
        assert rank[SessionState.NEEDS_REVIEW] == 2

    def test_registry_applies_custom_order(self, claude_dir, tmp_path, now):
        # one working (via recent record), one needs-review
        TranscriptBuilder(SID1, "/proj/a").user("go", ts=ts_ago(2)).write(
            claude_dir, mtime=now - 2
        )
        TranscriptBuilder(SID2, "/proj/a").user("go").assistant_text("done").write(
            claude_dir, mtime=now - 900
        )
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/a", "2026-08-18T09:00:00+00:00")
        store.track(SID2, "/proj/a", "2026-08-18T09:00:00+00:00")
        tmux = FakeTmux()
        tmux.sessions.append(
            TmuxSession(name="a", created=now - 60, activity=now, attached=False,
                        pane_pid=1, pane_path="/proj/a", socket="claude")
        )
        registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
        snap = registry.refresh(now=now)
        working = snap.by_id(SID1)
        review = snap.by_id(SID2)
        assert working.attention_rank > review.attention_rank  # default: review first
        # flip: working outranks everything
        store.set_setting("state_order", ["working"])
        snap = registry.refresh(now=now)
        assert snap.by_id(SID1).attention_rank < snap.by_id(SID2).attention_rank


async def test_priority_tab_reorders_and_persists(claude_dir, tmp_path):
    from conftest import render_text

    store = Store.load(tmp_path / "state.json")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        await pilot.press("2")  # Priority tab
        await pilot.pause()
        from textual.widgets import OptionList

        priority = app.screen.query_one("#priority-list", OptionList)
        assert priority.has_focus
        first = priority.get_option_at_index(0).id
        assert first == "needs input"
        await pilot.press("J")  # move it down one
        await pilot.pause(0.1)
        saved = store.get_setting("state_order")
        assert saved[0] == "needs review" and saved[1] == "needs input"
        assert Store.load(store.path).get_setting("state_order")[0] == "needs review"
        await pilot.press("0")  # reset to default
        await pilot.pause(0.1)
        assert store.get_setting("state_order")[0] == "needs input"


class TestWorkDir:
    def test_work_dir_follows_latest_cwd(self, claude_dir, now):
        from cagents.sessions import SessionView
        from cagents.store import TrackedSession as TS

        b = TranscriptBuilder(SID1, "/proj/repo")
        b.user("start here")
        b.cwd = "/proj/repo-worktrees/feature-x"  # Claude entered a worktree
        b.assistant_text("now working in the worktree")
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 10))
        assert parsed.cwd == "/proj/repo"  # first: stable grouping
        assert parsed.last_cwd == "/proj/repo-worktrees/feature-x"
        view = SessionView(
            session_id=SID1, tracked=TS(SID1, "/proj/repo", "x"),
            parsed=parsed, state=SessionState.NEEDS_REVIEW, live=False,
        )
        assert view.project_dir == "/proj/repo"
        assert view.work_dir == "/proj/repo-worktrees/feature-x"
