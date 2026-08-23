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
        store.clear_waiting(SID1, finished_reason="merged")
        got = Store.load(path).sessions[SID1]
        assert got.waiting_since == "" and got.finished_reason == "merged"

    def test_mark_external_update_roundtrip(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = Store.load(path)
        store.track(SID1, "/proj/a", "2026-08-18T09:00:00+00:00")
        store.set_waiting(SID1, "2026-08-18T10:00:00+00:00", "https://x/pull/1")
        store.mark_external_update(SID1, "2026-08-18T11:00:00+00:00", "pr updated")
        got = Store.load(path).sessions[SID1]
        # reopened out of waiting, into its own state — not a generic verdict
        assert got.waiting_since == ""
        assert got.finished_reason == "pr updated"
        assert got.external_update_since == "2026-08-18T11:00:00+00:00"


# ------------------------------------------------ prompt / state hardening ---


def test_prompt_detection_requires_dialog_signature():
    assert pane_shows_prompt("Do you want me to also refactor the parser?") is False
    assert pane_shows_prompt("Steps:\n 1. build\n 2. deploy") is False
    assert pane_shows_prompt("Do you want to proceed?\n❯ 1. Yes\n  2. No") is True


def test_ask_user_question_dialog_is_a_prompt():
    # Replicated live: AskUserQuestion's question text is model-composed,
    # not a fixed phrase ("Do you prefer tabs or spaces?") — it never
    # matches the human-phrase markers. Only its menu footer is constant.
    pane = (
        "Do you prefer tabs or spaces for indentation?\n\n"
        "❯ 1. Tabs\n"
        "     Use tab characters for indentation\n"
        "  2. Spaces\n"
        "     Use space characters for indentation\n\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel"
    )
    assert pane_shows_prompt(pane) is True


def test_arbitrary_question_text_alone_is_still_not_a_prompt():
    # The footer marker must still require the numbered-choice cursor —
    # conversational text that happens to ask a free-form question isn't
    # enough on its own.
    assert pane_shows_prompt("Do you prefer tabs or spaces? Esc to cancel if unsure.") is False


def test_extract_prompt_question():
    pane = "│ Do you want to proceed? │\n│ ❯ 1. Yes │"
    assert "Do you want to proceed?" in extract_prompt_question(pane)
    assert extract_prompt_question("just working…") == ""


def _tracked(**kw) -> TrackedSession:
    return TrackedSession(SID1, "/proj/a", "2026-08-18T09:00:00+00:00", **kw)


class TestNewStates:
    def _parse(self, claude_dir, builder):
        return parse_session_file(builder.write(claude_dir, mtime=time.time() - 300))

    def test_monitor_ack_yields_monitoring_until_expiry(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("watch the deploy")
        b.assistant_tool_use("t1", "Monitor", {"description": "watch deploy"})
        b.raw_tool_result(
            "t1",
            "Monitor started (task abc123, timeout 600000ms). "
            "You will be notified on each event.",
            ts=ts_ago(60),
        )
        b.assistant_text("Watching the deploy now.", ts=ts_ago(55))
        parsed = self._parse(claude_dir, b)
        assert parsed.monitor_running(now) is True
        state, detail = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.MONITORING
        assert "monitor" in detail.lower()
        # ...and the timeout is a hard upper bound: expired -> plain review
        assert parsed.monitor_running(now + 601) is False
        state, _ = derive_state(parsed, _tracked(), live=True, now=now + 601)
        assert state == SessionState.NEEDS_REVIEW

    def test_monitor_survives_new_messages_but_not_timeout_notice(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("watch it")
        b.assistant_tool_use("t1", "Monitor", {"description": "x"})
        b.raw_tool_result(
            "t1", "Monitor started (task mid42, timeout 900000ms).", ts=ts_ago(120)
        )
        # a NEW human exchange does not stop the monitor
        b.user("also do this other thing", ts=ts_ago(100))
        b.assistant_text("Done with the other thing.", ts=ts_ago(95))
        parsed = self._parse(claude_dir, b)
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.MONITORING
        # the terminal notification ends it
        b.raw(
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": ts_ago(50), "sessionId": SID1,
             "content": "<task-notification> <task-id>mid42</task-id> "
                        "<summary>Monitor event</summary> "
                        "<event>[Monitor timed out — re-arm if needed.]</event> "
                        "</task-notification>"}
        )
        b.assistant_text("Noted the timeout.", ts=ts_ago(45))
        parsed = self._parse(claude_dir, b)
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_monitor_normal_completion_ends_it_before_the_timeout_deadline(self, claude_dir, now):
        # Real bug, confirmed live: a Monitor that finishes on its own
        # (target command completed, not a timeout) sends a
        # task-notification with the exact same "<status>completed</status>"
        # shape as a background command's — but _scan_lifecycle only used
        # to clear active_background on that shape, never active_monitors.
        # Left unfixed, monitor_running() stays True until the *original*
        # timeout deadline (here 60s), long after the monitor actually
        # ended — this asserts it's cleared immediately instead.
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("watch it")
        b.assistant_tool_use("t1", "Monitor", {"description": "wait for target-done"})
        # a long timeout, well beyond "now" — so this test actually
        # distinguishes "cleared by the completion notification" from
        # "just happened to naturally expire" (a short timeout coinciding
        # with `now` would pass either way and prove nothing)
        b.raw_tool_result(
            "t1", "Monitor started (task bw0tx7k9t, timeout 600000ms).", ts=ts_ago(60)
        )
        b.assistant_text("Monitor is running.", ts=ts_ago(55))
        # the real, exact notification shape a normal completion sends
        # (verified against a live haiku session — not a timeout message)
        b.raw(
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": ts_ago(50), "sessionId": SID1,
             "content": "<task-notification>\n<task-id>bw0tx7k9t</task-id>\n"
                        "<tool-use-id>toolu_01SZpBPe3sPDrB8utqewPCHp</tool-use-id>\n"
                        "<output-file>/tmp/x/tasks/bw0tx7k9t.output</output-file>\n"
                        "<status>completed</status>\n"
                        "<summary>Monitor \"wait for target-done\" stream ended</summary>\n"
                        "</task-notification>"}
        )
        b.assistant_text("Monitor completed successfully.", ts=ts_ago(45))
        parsed = self._parse(claude_dir, b)
        assert parsed.monitor_running(now) is False
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_background_ack_yields_background(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("run the long build")
        b.assistant_tool_use("t1", "Bash", {"command": "make", "run_in_background": True})
        b.raw_tool_result("t1", "Command running in background with ID: bv49j5apt. "
                                "Output is being written to: /tmp/x.output")
        b.assistant_text("Build started in the background.", ts="2026-08-18T10:00:10.000Z")
        parsed = self._parse(claude_dir, b)
        assert parsed.background_active is True
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.BACKGROUND

    def test_background_survives_new_messages_until_completion(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("run it")
        b.assistant_tool_use("t1", "Bash", {"command": "make", "run_in_background": True})
        b.raw_tool_result("t1", "Command running in background with ID: bv49j5apt. …")
        # you message it, it responds — the shell is STILL running
        b.user("quick question meanwhile", ts="2026-08-18T10:05:00.000Z")
        b.assistant_text("Answer.", ts="2026-08-18T10:05:10.000Z")
        parsed = self._parse(claude_dir, b)
        assert parsed.background_active is True
        state, _ = derive_state(parsed, _tracked(), live=True, now=now)
        assert state == SessionState.BACKGROUND  # not review!
        # the completion notification ends it (real transcript format)
        b.raw(
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": "2026-08-18T10:20:00.000Z", "sessionId": SID1,
             "content": '<task-notification> <task-id>bv49j5apt</task-id> '
                        '<status>completed</status> <summary>Background command '
                        '"make" completed (exit code 0)</summary> </task-notification>'}
        )
        b.assistant_text("Build finished.", ts="2026-08-18T10:20:10.000Z")
        parsed = self._parse(claude_dir, b)
        assert parsed.background_active is False
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

    def test_finished_reason_shows_in_detail(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("ship it").assistant_text("PR is up.", ts="2026-08-18T10:00:00.000Z")
        parsed = self._parse(claude_dir, b)
        state, detail = derive_state(
            parsed, _tracked(finished_reason="pr closed"), live=False, now=now
        )
        assert state == SessionState.NEEDS_REVIEW
        assert detail == "pr closed"

    def test_external_update_derivation(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("ship it").assistant_text("PR is up.", ts="2026-08-18T10:00:00.000Z")
        parsed = self._parse(claude_dir, b)
        tracked = _tracked(
            external_update_since="2026-08-18T11:00:00+00:00", finished_reason="github comments"
        )
        state, detail = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.EXTERNAL_UPDATE
        assert detail == "github comments"

    def test_external_update_folds_in_non_comment_pr_changes(self, claude_dir, now):
        # e.g. someone pushed a commit or edited the PR with no new
        # comment/review — folded into the same bucket as comments, not a
        # generic needs-review.
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("ship it").assistant_text("PR is up.", ts="2026-08-18T10:00:00.000Z")
        parsed = self._parse(claude_dir, b)
        tracked = _tracked(
            external_update_since="2026-08-18T11:00:00+00:00", finished_reason="pr updated"
        )
        state, detail = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.EXTERNAL_UPDATE
        assert detail == "pr updated"

    def test_external_update_self_expires_on_new_activity(self, claude_dir, now):
        # the PR changed, then the human typed something new (last_timestamp
        # advances past external_update_since) -> back to a normal review,
        # no explicit clearing needed anywhere
        b = TranscriptBuilder(SID1, "/proj/a")
        b.user("ship it").assistant_text(
            "Addressed the feedback.", ts="2026-08-18T12:00:00.000Z"
        )
        parsed = self._parse(claude_dir, b)
        tracked = _tracked(external_update_since="2026-08-18T11:00:00+00:00")
        state, _ = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_external_update_outranks_plain_review(self):
        from cagents.sessions import ATTENTION_ORDER

        order = ATTENTION_ORDER
        assert order[SessionState.NEEDS_INPUT] < order[SessionState.EXTERNAL_UPDATE]
        assert order[SessionState.EXTERNAL_UPDATE] < order[SessionState.NEEDS_REVIEW]


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
        assert tracked.external_update_since != ""
        view = app.snapshot.by_id(SID1)
        assert view.state == SessionState.EXTERNAL_UPDATE
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
        assert tracked.finished_reason == "merged"
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


async def test_toasts_render_bottom_left(world):
    from textual.widgets._toast import Toast

    app, store, _ = world
    store.set_setting("notifications", True)
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await pilot.pause()
        app.notify("test toast")
        await pilot.pause(0.2)
        toasts = list(app.query(Toast))
        assert toasts
        # bottom-left: hugs x=0, not the far right edge
        assert toasts[0].region.x == 0


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
        assert rank[SessionState.EXTERNAL_UPDATE] == 2
        assert rank[SessionState.NEEDS_REVIEW] == 3

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
        assert saved[0] == "external update" and saved[1] == "needs input"
        assert Store.load(store.path).get_setting("state_order")[0] == "external update"
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


class TestEventDerivation:
    """Hook events (Notification/Stop/UserPromptSubmit) are authoritative
    for sessions cagents spawned — validated against a real haiku session
    (35s quiet foreground tool, zero false needs-input)."""

    def _parsed(self, claude_dir, now, pending=True):
        b = TranscriptBuilder(SID1, "/proj/a").user("go", ts=ts_ago(30))
        if pending:
            b.assistant_tool_use("t1", "Bash", {"command": "make"}, ts=ts_ago(28))
        return parse_session_file(b.write(claude_dir, mtime=now - 28))

    def test_submit_event_holds_working_through_quiet_tool(self, claude_dir, now):
        parsed = self._parsed(claude_dir, now)
        events = {"UserPromptSubmit": now - 29}
        state, detail = derive_state(
            parsed, _tracked(), live=True, now=now, events=events
        )
        assert state == SessionState.WORKING
        assert detail == "running Bash"

    def test_notification_event_means_needs_input(self, claude_dir, now):
        parsed = self._parsed(claude_dir, now)
        events = {"UserPromptSubmit": now - 29, "Notification": now - 5,
                  "message": "Claude needs your permission to use Bash"}
        state, detail = derive_state(
            parsed, _tracked(), live=True, now=now, events=events
        )
        assert state == SessionState.NEEDS_INPUT
        assert "permission" in detail

    def test_notification_consumed_by_newer_activity(self, claude_dir, now):
        # approval produced a tool_result newer than the notification
        b = TranscriptBuilder(SID1, "/proj/a").user("go", ts=ts_ago(30))
        b.assistant_tool_use("t1", "Bash", {"command": "make"}, ts=ts_ago(28))
        b.tool_result("t1", ts=ts_ago(3))
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 3))
        events = {"UserPromptSubmit": now - 29, "Notification": now - 10}
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state == SessionState.WORKING  # back to the turn

    def test_generic_idle_notification_after_stop_stays_needs_review(self, claude_dir, now):
        # Replicated live: Claude Code fires the exact same Notification
        # hook, with the exact same generic "Claude is waiting for your
        # input" message, purely as an idle "still waiting, come look"
        # nudge 60s after a turn already ended cleanly — there is no
        # tool call behind it. Must not read as a live blocking dialog.
        b = TranscriptBuilder(SID1, "/proj/a").user("summarize", ts=ts_ago(80))
        b.assistant_text("Summary: …", ts=ts_ago(66))
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 66))
        events = {
            "UserPromptSubmit": now - 80,
            "Stop": now - 66,
            "Notification": now - 6,
            "message": "Claude is waiting for your input",
        }
        state, detail = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state == SessionState.NEEDS_REVIEW
        assert detail == "finished, unreviewed"

    def test_idle_prompt_notification_type_is_never_blocking(self, claude_dir, now):
        # The authoritative fix: Claude Code's own notification_type field
        # says this is idle_prompt (its documented "just idle, nothing to
        # do" type) — never NEEDS_INPUT, no matter what pending_tool_use
        # says, and even with no Stop event recorded at all.
        parsed = self._parsed(claude_dir, now, pending=True)
        events = {
            "UserPromptSubmit": now - 29, "Notification": now - 5,
            "notification_type": "idle_prompt",
            "message": "Claude is waiting for your input",
        }
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state != SessionState.NEEDS_INPUT

    def test_permission_prompt_notification_type_is_blocking(self, claude_dir, now):
        parsed = self._parsed(claude_dir, now, pending=False)  # no pending tool in transcript
        events = {
            "UserPromptSubmit": now - 29, "Notification": now - 5,
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use Bash",
        }
        state, detail = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state == SessionState.NEEDS_INPUT
        assert "permission" in detail

    def test_unknown_notification_type_falls_back_to_pending_tool_heuristic(self, claude_dir, now):
        # A future Claude Code notification_type this build doesn't know
        # about yet must not silently become blocking by default.
        parsed = self._parsed(claude_dir, now, pending=False)
        events = {
            "UserPromptSubmit": now - 29, "Notification": now - 5,
            "notification_type": "some_new_type_from_a_future_release",
        }
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state != SessionState.NEEDS_INPUT

    def test_missing_notification_type_falls_back_to_pending_tool_heuristic(self, claude_dir, now):
        # Older Claude Code build without the field at all: same
        # behavior as before this fix (pending_tool_use decides).
        parsed = self._parsed(claude_dir, now, pending=True)
        events = {"UserPromptSubmit": now - 29, "Notification": now - 5,
                  "message": "Claude needs your permission to use Bash"}
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state == SessionState.NEEDS_INPUT

    def test_stop_event_finishes_immediately(self, claude_dir, now):
        b = TranscriptBuilder(SID1, "/proj/a").user("go", ts=ts_ago(10))
        b.assistant_text("done", ts=ts_ago(5))
        parsed = parse_session_file(b.write(claude_dir, mtime=now - 5))
        events = {"UserPromptSubmit": now - 9, "Stop": now - 4}
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, events=events)
        assert state == SessionState.NEEDS_REVIEW

    def test_no_events_falls_back_to_heuristics(self, claude_dir, now):
        parsed = self._parsed(claude_dir, now)
        state, _ = derive_state(parsed, _tracked(), live=True, now=now, events={})
        assert state == SessionState.WORKING  # heuristic path (pending tool)


class TestCtxEvent:
    def test_event_merges_and_records_message(self, tmp_path, monkeypatch):
        import io

        from cagents.ctx import do_event, read_context

        path = tmp_path / "events" / "sid.json"
        assert do_event("UserPromptSubmit", path) == 0
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO('{"message": "Claude needs your permission", "notification_type": "permission_prompt"}'),
        )
        assert do_event("Notification", path) == 0
        events = read_context(path)
        assert events["UserPromptSubmit"] > 0
        assert events["Notification"] >= events["UserPromptSubmit"]
        assert events["message"] == "Claude needs your permission"
        assert events["notification_type"] == "permission_prompt"

    def test_event_without_notification_type_does_not_stick_from_before(self, tmp_path, monkeypatch):
        import io

        from cagents.ctx import do_event, read_context

        path = tmp_path / "events" / "sid.json"
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO('{"message": "first", "notification_type": "permission_prompt"}'),
        )
        do_event("Notification", path)
        monkeypatch.setattr("sys.stdin", io.StringIO('{"message": "second"}'))
        do_event("Notification", path)
        events = read_context(path)
        assert "notification_type" not in events


class TestDiffMode:
    def test_branch_pipeline_prefers_remote_refs(self):
        from cagents.ctx import diff_popup_command

        command = diff_popup_command("/proj/x", mode="branch")
        assert "origin/main origin/master main master" in command
        assert "merge-base" in command and "less -R" in command

    def test_uncommitted_pipeline(self):
        from cagents.ctx import diff_popup_command

        command = diff_popup_command("/proj/x", mode="uncommitted")
        assert "git diff --color=always HEAD" in command
        assert "merge-base" not in command

    def test_setting_cycles_and_reaches_context(self, tmp_path):
        store = Store.load(tmp_path / "state.json")
        assert store.get_setting("diff_mode") == "branch"
        store.set_setting("diff_mode", "uncommitted")
        assert Store.load(store.path).get_setting("diff_mode") == "uncommitted"
        store.set_setting("diff_mode", 5)  # wrong type ignored
        assert store.get_setting("diff_mode") == "uncommitted"


async def test_pr_poll_reacts_to_close_and_any_update(world):
    app, store, tmux = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # closed without merge -> re-alert as "pr closed"
        store.set_waiting(SID1, "2026-08-19T10:00:00+00:00", "https://x/pull/1")
        app.gh_runner = lambda args, cwd=None: (
            '{"state": "CLOSED", "mergedAt": null, "comments": [], "reviews": [],'
            ' "updatedAt": "2026-08-19T09:00:00+00:00"}'
        )
        app._poll_waiting_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[SID1]
        assert tracked.waiting_since == "" and tracked.finished_reason == "pr closed"
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW

        # any other change (e.g. pushed commits): updatedAt newer -> folded
        # into the same "external update" bucket as comments, not a plain
        # needs-review
        store.set_waiting(SID1, "2026-08-19T11:00:00+00:00", "https://x/pull/1")
        app.gh_runner = lambda args, cwd=None: (
            '{"state": "OPEN", "mergedAt": null, "comments": [], "reviews": [],'
            ' "updatedAt": "2026-08-19T12:00:00+00:00"}'
        )
        app._poll_waiting_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[SID1]
        assert tracked.waiting_since == "" and tracked.finished_reason == "pr updated"
        assert tracked.external_update_since != ""
        assert app.snapshot.by_id(SID1).state == SessionState.EXTERNAL_UPDATE
        assert app.snapshot.by_id(SID1).state_detail == "pr updated"

        # no change at all -> stays parked
        store.set_waiting(SID1, "2026-08-19T13:00:00+00:00", "https://x/pull/1")
        app.gh_runner = lambda args, cwd=None: (
            '{"state": "OPEN", "mergedAt": null, "comments": [], "reviews": [],'
            ' "updatedAt": "2026-08-19T12:00:00+00:00"}'
        )
        app._poll_waiting_prs()
        await pilot.pause(0.3)
        assert store.sessions[SID1].waiting_since != ""


# ------------------------------------------------------- conversation search ---


async def test_search_action_gated_by_setting(world):
    app, store, _ = world
    assert store.get_setting("conversation_search") is False
    captured = []
    import textual.app as textual_app

    original = textual_app.App.notify

    def spy(self, message, **kwargs):
        captured.append(kwargs.get("severity", "information"))

    textual_app.App.notify = spy
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause(0.1)
            from cagents.modals import SearchModal

            assert not isinstance(app.screen, SearchModal)
            assert "warning" in captured
    finally:
        textual_app.App.notify = original


async def test_search_flow_tracks_and_selects_the_chosen_result(world, claude_dir, now):
    app, store, tmux = world
    store.set_setting("conversation_search", True)
    sid = "77777777-7777-7777-7777-777777777777"
    TranscriptBuilder(sid, "/proj/found-me").ai_title("Found session").user(
        "go"
    ).assistant_text("the unobtainium widget needs recalibration").write(
        claude_dir, mtime=now - 10
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause(0.1)
        from cagents.modals import SearchModal

        assert isinstance(app.screen, SearchModal)
        await pilot.press(*"unobtainium")
        await pilot.press("enter")
        await pilot.pause(0.5)  # background search worker
        await pilot.press("enter")  # select the (only) result
        await pilot.pause(0.2)
        assert sid in store.sessions
        assert store.sessions[sid].project_dir == "/proj/found-me"
        assert app.selected_session_id == sid
