"""Tests for fork, pause/wake, waiting-on-PR-review, desktop notifications,
statusline, split shell, and the rich diff launcher."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import SID1, SID2, TranscriptBuilder, ts_ago
from test_app import FakeTmux, render_text

from cagents.app import CagentsApp
from cagents.notifier import read_select_request
from cagents.sessions import SessionRegistry, SessionState, derive_state
from cagents.store import Store, TrackedSession
from cagents.tmuxctl import TmuxSession
from cagents.views import SessionList
from cagents.wake import (
    WakeEngine,
    build_wake_prompt,
    extract_script,
    iso_in,
    parse_duration,
)


# ------------------------------------------------------ waiting on review --


class TestWaitingOnReview:
    def _parsed(self, claude_dir, ts="2026-08-17T10:00:00.000Z"):
        from cagents.claude_data import parse_session_file

        b = TranscriptBuilder(SID1, "/proj/a").user("go").assistant_text("done", ts=ts)
        return parse_session_file(b.write(claude_dir, mtime=time.time() - 300))

    def _tracked(self, **kw):
        return TrackedSession(SID1, "/proj/a", "2026-08-17T09:00:00+00:00", **kw)

    def test_waiting_after_mark(self, claude_dir, now):
        parsed = self._parsed(claude_dir)
        tracked = self._tracked(waiting_pr_since="2026-08-17T11:00:00+00:00")
        state, detail = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.WAITING_ON_REVIEW
        assert detail == "waiting on review"

    def test_new_activity_realerts(self, claude_dir, now):
        parsed = self._parsed(claude_dir, ts="2026-08-17T12:00:00.000Z")  # after mark
        tracked = self._tracked(waiting_pr_since="2026-08-17T11:00:00+00:00")
        state, _ = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.NEEDS_REVIEW

    def test_reviewed_beats_waiting(self, claude_dir, now):
        parsed = self._parsed(claude_dir)
        tracked = self._tracked(
            waiting_pr_since="2026-08-17T11:00:00+00:00",
            reviewed_at="2026-08-17T11:30:00+00:00",
        )
        state, _ = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.DONE

    def test_merged_note_shows_done_merged(self, claude_dir, now):
        parsed = self._parsed(claude_dir)
        tracked = self._tracked(pr_status_note="merged")
        state, detail = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.DONE
        assert detail == "merged"

    def test_reopened_note_shows_github_comments(self, claude_dir, now):
        parsed = self._parsed(claude_dir)
        tracked = self._tracked(pr_status_note="github comments")
        state, detail = derive_state(parsed, tracked, live=False, now=now)
        assert state == SessionState.NEEDS_REVIEW
        assert detail == "github comments"

    def test_attention_order_places_waiting_on_review(self):
        from cagents.sessions import ATTENTION_ORDER

        order = ATTENTION_ORDER
        assert (
            order[SessionState.NEEDS_REVIEW]
            < order[SessionState.WAITING_ON_REVIEW]
            < order[SessionState.WORKING]
        )


# ------------------------------------------------------------ wake engine --


class TestWake:
    def test_parse_duration(self):
        assert parse_duration("30m") == 1800
        assert parse_duration(" 2d ") == 172800
        assert parse_duration("1w") == 604800
        assert parse_duration("4H") == 14400
        assert parse_duration("when CI passes") is None
        assert parse_duration("") is None

    def test_extract_script(self):
        reply = "Here you go:\n```sh\ngh pr checks --required | grep -q pass\n```\nDone."
        script = extract_script(reply)
        assert script.startswith("#!/bin/sh\n")
        assert "gh pr checks" in script
        with pytest.raises(ValueError):
            extract_script("")

    def test_build_wake_prompt_mentions_rules(self):
        prompt = build_wake_prompt("PR approved", "/proj/x")
        assert "PR approved" in prompt and "/proj/x" in prompt
        assert "exit 0" in prompt and "read-only" in prompt

    def _store(self, tmp_path) -> Store:
        return Store.load(tmp_path / "state.json")

    def test_timer_wakes(self, tmp_path):
        store = self._store(tmp_path)
        todo = store.add_todo("t", "2026-08-17T09:00:00+00:00")
        now = time.time()
        store.pause_todo(todo.todo_id, paused_at="x", wake_at=iso_in(60, now))
        engine = WakeEngine(store)
        assert engine.tick(now=now + 30).woken == []
        report = engine.tick(now=now + 61)
        assert report.woken == [(todo.todo_id, "timer elapsed")]
        assert not store.todos[todo.todo_id].paused

    def test_script_wake_and_interval(self, tmp_path):
        store = self._store(tmp_path)
        todo = store.add_todo("t", "2026-08-17T09:00:00+00:00")
        script = tmp_path / "check.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        store.pause_todo(todo.todo_id, paused_at="x", wake_criteria="CI green",
                         wake_script=str(script))
        calls = []

        def runner(path):
            calls.append(path)
            return len(calls) >= 2  # false first, true second

        engine = WakeEngine(store, run_script=runner)
        now = time.time()
        assert engine.tick(now=now).woken == []
        # within the 5-minute interval: script NOT rerun
        engine.tick(now=now + 60)
        assert len(calls) == 1
        report = engine.tick(now=now + 400)
        assert len(calls) == 2
        assert report.woken == [(todo.todo_id, "CI green")]

    def test_auto_pause_idle_todo(self, tmp_path):
        store = self._store(tmp_path)
        todo = store.add_todo("old", "2026-08-01T09:00:00+00:00")
        engine = WakeEngine(store)
        now = time.time()
        report = engine.tick(now=now, last_activity=lambda t: now - 8 * 86400)
        assert report.auto_paused == [todo.todo_id]
        assert store.todos[todo.todo_id].paused
        assert "auto-paused" in store.todos[todo.todo_id].wake_criteria

    def test_auto_pause_respects_setting_and_activity(self, tmp_path):
        store = self._store(tmp_path)
        todo = store.add_todo("busy", "2026-08-01T09:00:00+00:00")
        now = time.time()
        engine = WakeEngine(store)
        # recent activity -> stays open
        assert engine.tick(now=now, last_activity=lambda t: now - 3600).auto_paused == []
        # disabled -> stays open even when ancient
        store.set_setting("auto_pause_days", 0)
        assert engine.tick(now=now, last_activity=lambda t: now - 90 * 86400).auto_paused == []
        assert not store.todos[todo.todo_id].paused


# ------------------------------------------------------------- UI world ---


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Fix auth").user("go").assistant_text(
        "All fixed."
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


async def test_fork_flow(world, monkeypatch):
    app, store, tmux = world
    sent = []
    tmux.send_text = lambda name, text, submit=True: sent.append((name, text))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await pilot.pause()
        await pilot.press(*"try the async approach instead")
        await pilot.press("enter")
        await pilot.pause(0.4)
        # forked with the right flags, from the original session
        directory, args, new_id = tmux.created[-1]
        assert args[0:3] == ["--resume", SID1, "--fork-session"]
        assert args[3] == "--session-id" and args[4] == new_id
        assert new_id != SID1
        # tracked + named after the prompt; original untouched
        assert store.sessions[new_id].label == "try the async approach instead"
        assert SID1 in store.sessions
        assert sent and sent[0][1] == "try the async approach instead"


async def test_waiting_review_key_without_pr_link_warns(world):
    app, store, _ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW
        await pilot.press("w")
        await pilot.pause(0.2)
        # no PR link in this transcript -> refused, no change
        assert store.sessions[SID1].waiting_pr_since == ""


async def test_waiting_review_key_marks_and_toggles_off(claude_dir, tmp_path, now, monkeypatch):
    from cagents import gitops

    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Fix auth").user("go").raw(
        {"type": "pr-link", "sessionId": SID1, "prNumber": 9,
         "prUrl": "https://github.com/x/y/pull/9"}
    ).assistant_text("All fixed.").write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)

    monkeypatch.setattr(
        gitops, "github_pr_status",
        lambda directory, gh_bin="gh": gitops.PrStatus(number=9, state="OPEN", comment_count=2),
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW
        await pilot.press("w")
        await pilot.pause(0.3)
        assert store.sessions[SID1].waiting_pr_since != ""
        assert store.sessions[SID1].waiting_pr_baseline_comments == 2
        assert app.snapshot.by_id(SID1).state == SessionState.WAITING_ON_REVIEW

        await pilot.press("w")  # toggle back off
        await pilot.pause(0.2)
        assert store.sessions[SID1].waiting_pr_since == ""
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW


async def test_pr_status_tick_marks_merged(world, monkeypatch):
    from cagents import gitops

    app, store, _ = world
    store.set_waiting_on_pr(SID1, "2026-08-17T11:00:00+00:00", 1)
    monkeypatch.setattr(
        gitops, "github_pr_status",
        lambda directory, gh_bin="gh": gitops.PrStatus(number=9, state="MERGED", comment_count=1),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.snapshot.by_id(SID1).state == SessionState.WAITING_ON_REVIEW
        app._check_pr_statuses()
        await pilot.pause(0.3)
        assert store.sessions[SID1].pr_status_note == "merged"
        assert store.sessions[SID1].waiting_pr_since == ""
        assert app.snapshot.by_id(SID1).state == SessionState.DONE


async def test_pr_status_tick_reopens_on_new_comments(world, monkeypatch):
    from cagents import gitops

    app, store, _ = world
    store.set_waiting_on_pr(SID1, "2026-08-17T11:00:00+00:00", 1)
    monkeypatch.setattr(
        gitops, "github_pr_status",
        lambda directory, gh_bin="gh": gitops.PrStatus(number=9, state="OPEN", comment_count=2),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.snapshot.by_id(SID1).state == SessionState.WAITING_ON_REVIEW
        app._check_pr_statuses()
        await pilot.pause(0.3)
        assert store.sessions[SID1].pr_status_note == "github comments"
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW


async def test_pr_status_tick_leaves_unchanged_pr_alone(world, monkeypatch):
    from cagents import gitops

    app, store, _ = world
    store.set_waiting_on_pr(SID1, "2026-08-17T11:00:00+00:00", 2)
    monkeypatch.setattr(
        gitops, "github_pr_status",
        lambda directory, gh_bin="gh": gitops.PrStatus(number=9, state="OPEN", comment_count=2),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app._check_pr_statuses()
        await pilot.pause(0.3)
        assert store.sessions[SID1].waiting_pr_since != ""
        assert app.snapshot.by_id(SID1).state == SessionState.WAITING_ON_REVIEW


async def test_pause_with_timer(world):
    app, store, _ = world
    todo = store.add_todo("later thing", "2026-08-17T09:00:00+00:00")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press(*"2d")
        await pilot.press("enter")
        await pilot.pause(0.2)
        got = store.todos[todo.todo_id]
        assert got.paused and got.wake_at != ""
        rows = "\n".join(
            render_text(app.query_one("#todos-list", SessionList).get_option_at_index(i).prompt)
            for i in range(app.query_one("#todos-list", SessionList).option_count)
        )
        assert "── paused" in rows and "wakes in" in rows
        # p again unpauses
        await pilot.press("p")
        await pilot.pause(0.2)
        assert not store.todos[todo.todo_id].paused


async def test_pause_with_criteria_generates_script(world):
    app, store, _ = world

    class FakeRunner:
        def run(self, prompt):
            assert "the PR is approved" in prompt
            return "```sh\nexit 1\n```"

    app.claude_runner = FakeRunner()
    todo = store.add_todo("pr thing", "2026-08-17T09:00:00+00:00", "/proj/alpha")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press(*"the PR is approved")
        await pilot.press("enter")
        await pilot.pause(0.4)  # worker + confirm modal
        from cagents.modals import ScriptConfirmModal

        assert isinstance(app.screen, ScriptConfirmModal)
        await pilot.press("y")
        await pilot.pause(0.2)
        got = store.todos[todo.todo_id]
        assert got.paused and got.wake_criteria == "the PR is approved"
        assert got.wake_script.endswith(f"{todo.todo_id}.sh")
        assert Path(got.wake_script).read_text("utf-8").startswith("#!/bin/sh")


async def test_desktop_notification_on_transition(world, monkeypatch, tmp_path):
    app, store, tmux = world
    store.set_setting("desktop_notifications", True)
    fired = []
    monkeypatch.setattr(
        "cagents.app.notify_desktop",
        lambda title, msg, sid, state_dir, **kw: fired.append((title, sid)),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert fired == []  # startup snapshot never notifies
        # simulate: session was working, now needs review
        app._prev_states[SID1] = SessionState.WORKING
        app.apply_snapshot(app.registry.refresh())
        await pilot.pause(0.3)
        assert fired and fired[0][1] == SID1


async def test_click_select_request(world, tmp_path):
    app, store, _ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("3")  # kanban — a view where selection isn't obvious
        await pilot.pause()
        (store.path.parent / "select-request").write_text(SID1 + "\n")
        app.apply_snapshot(app.registry.refresh())
        await pilot.pause()
        assert app.active_view_id == "queue"  # jumped to a list view
        assert app.selected_session_id == SID1
        assert read_select_request(store.path.parent) is None  # consumed


async def test_rich_diff_falls_back_without_lazygit(world, monkeypatch, tmp_path):
    app, store, tmux = world
    monkeypatch.setattr("cagents.app.shutil.which", lambda name: None)
    project = tmp_path / "diffdir"
    project.mkdir()
    store.sessions[SID1].project_dir = str(project)
    opened = []
    app._diff_worker = lambda directory: opened.append(directory)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.by_id(SID1)
        if view and view.parsed:
            view.parsed.cwd = str(project)
        await pilot.press("V")
        await pilot.pause(0.2)
        assert opened == [str(project)]  # built-in diff took over


async def test_split_shell_uses_sidecar(world, tmp_path):
    from test_sidecar import FakeOuterTmux
    from cagents.sidecar import Sidecar

    app, store, _ = world
    outer = FakeOuterTmux()
    app.sidecar = Sidecar(runner=outer, own_pane="%0")
    project = tmp_path / "realdir"
    project.mkdir()
    store.sessions[SID1].project_dir = str(project)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.by_id(SID1)
        if view and view.parsed:
            view.parsed.cwd = str(project)
        await pilot.press("t")
        await pilot.pause()
        split = [c for c in outer.calls if c[0] == "split-window"]
        assert split and "-c" in split[0] and str(project) in split[0]


def test_statusline_commands_roundtrip():
    from cagents.tmuxctl import TmuxClient

    calls = []

    class T(TmuxClient):
        def _run(self, *args, timeout=5.0):
            calls.append(args)

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

    t = T()
    t.session_statusline_on("mysess")
    assert ("set", "-t", "=mysess", "status", "on") in calls
    on_count = len(calls)
    t.session_statusline_off("mysess")
    off = calls[on_count:]
    assert all(c[1] == "-u" for c in off)  # every option unset, none forgotten
    assert len(off) == on_count
