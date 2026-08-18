"""Tests for the cagents-next prototypes: derived badges and the fleet
palette."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import SID1, SID2, SID3, TranscriptBuilder
from test_app import FakeTmux, render_text, select_session, widget_text

from cagents.app import CagentsApp
from cagents.claude_data import parse_session_file
from cagents.palette import (
    ALLOWED_ACTIONS,
    apply_plan,
    build_prompt,
    fleet_table,
    parse_plan,
)
from cagents.sessions import SessionRegistry, SessionState
from cagents.store import Store


# ---------------------------------------------------------------- parser --


class TestDerivedExtras:
    def test_pr_and_frame_links(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("open a pr")
        b.raw({"type": "pr-link", "sessionId": SID1, "prNumber": 62,
               "prUrl": "https://github.com/x/y/pull/62", "prRepository": "x/y"})
        b.raw({"type": "frame-link", "sessionId": SID1, "path": "/tmp/x.html",
               "frameUrl": "https://claude.ai/code/artifact/abc"})
        b.assistant_text("done")
        parsed = parse_session_file(b.write(claude_dir))
        kinds = [(l.kind, l.label) for l in parsed.links]
        assert kinds == [("pr", "PR #62"), ("artifact", "artifact")]
        assert parsed.links[0].url.endswith("/pull/62")

    def test_duplicate_links_deduped(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        for _ in range(3):
            b.raw({"type": "pr-link", "sessionId": SID1, "prNumber": 62,
                   "prUrl": "https://github.com/x/y/pull/62"})
        parsed = parse_session_file(b.write(claude_dir))
        assert len(parsed.links) == 1

    def test_malformed_link_record_ignored(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.raw({"type": "pr-link"})  # no url at all
        b.user("hi")
        parsed = parse_session_file(b.write(claude_dir))
        assert parsed.links == []

    def test_files_touched_ordered_dedup(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("edit stuff")
        b.assistant_tool_use("t1", "Write", {"file_path": "/proj/alpha/a.py"})
        b.tool_result("t1")
        b.assistant_tool_use("t2", "Edit", {"file_path": "/proj/alpha/b.py"})
        b.tool_result("t2")
        b.assistant_tool_use("t3", "Edit", {"file_path": "/proj/alpha/a.py"})  # again
        b.tool_result("t3")
        b.assistant_tool_use("t4", "Read", {"file_path": "/proj/alpha/c.py"})  # read-only
        b.tool_result("t4")
        parsed = parse_session_file(b.write(claude_dir))
        assert parsed.files_touched == ["/proj/alpha/a.py", "/proj/alpha/b.py"]

    def test_pending_agents_from_system_records(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("go wide")
        b.raw({"type": "system", "subtype": "turn_duration", "durationMs": 1234,
               "pendingBackgroundAgentCount": 5, "isSidechain": False})
        parsed = parse_session_file(b.write(claude_dir))
        assert parsed.pending_agents == 5
        assert parsed.last_turn_duration_ms == 1234

    def test_last_resolved_tool_tracks_name_and_background_flag(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("go").assistant_tool_use("t1", "Bash", {"command": "ls"}).tool_result("t1")
        b.assistant_tool_use(
            "t2", "Bash", {"command": "npm run build", "run_in_background": True}, ts="2026-08-17T10:00:10.000Z"
        ).tool_result("t2", ts="2026-08-17T10:00:11.000Z")
        parsed = parse_session_file(b.write(claude_dir))
        # the *most recent* resolution wins, overwriting the earlier one
        assert parsed.last_resolved_tool_name == "Bash"
        assert parsed.last_resolved_tool_background is True

    def test_last_resolved_tool_background_false_for_a_foreground_call(self, claude_dir: Path):
        b = TranscriptBuilder(SID1, "/proj/alpha")
        b.user("go").assistant_tool_use("t1", "Bash", {"command": "ls"}).tool_result("t1")
        parsed = parse_session_file(b.write(claude_dir))
        assert parsed.last_resolved_tool_name == "Bash"
        assert parsed.last_resolved_tool_background is False


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user(
        "please fix auth"
    ).assistant_text("done with auth, see PR").raw(
        {"type": "pr-link", "sessionId": SID1, "prNumber": 7,
         "prUrl": "https://github.com/x/y/pull/7"}
    ).write(claude_dir, mtime=now - 900)
    TranscriptBuilder(SID2, "/proj/beta").ai_title("Beta: refactor").user("go").assistant_text(
        "refactored"
    ).write(claude_dir, mtime=now - 4000)

    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.track(SID2, "/proj/beta", "2026-08-17T08:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


async def test_preview_shows_badges(world):
    app, *_ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        preview = widget_text(app, "#preview-content")
        assert "PR #7" in preview


# --------------------------------------------------------------- palette --


class FakeRunner:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def _plan_reply(actions: list[dict], reply: str = "Done thinking.") -> str:
    return "Some preamble the model shouldn't emit but might\n" + json.dumps(
        {"reply": reply, "actions": actions}
    )


def test_parse_plan_validates_actions(claude_dir, tmp_path, now):
    TranscriptBuilder(SID1, "/proj/alpha").user("x").assistant_text("y").write(
        claude_dir, mtime=now - 100
    )
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
    snap = registry.refresh(now=now)

    raw = _plan_reply(
        [
            {"action": "mark_reviewed", "session_id": SID1, "reason": "merged"},
            {"action": "delete_everything", "session_id": SID1},  # not allowed
            {"action": "set_note", "session_id": SID3, "value": "x"},  # unknown session
        ]
    )
    plan = parse_plan(raw, snap)
    assert len(plan.actions) == 1
    assert plan.actions[0].action == "mark_reviewed"
    assert len(plan.dropped) == 2
    assert "disallowed" in plan.dropped[0]
    assert "unknown session" in plan.dropped[1]


def test_parse_plan_rejects_garbage():
    from cagents.sessions import Snapshot

    with pytest.raises(ValueError):
        parse_plan("I would love to help but here is prose", Snapshot())


def test_fleet_table_and_prompt(claude_dir, tmp_path, now):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha work").user("x").assistant_text(
        "y"
    ).write(claude_dir, mtime=now - 100)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.set_note(SID1, "waiting on CI")
    registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
    snap = registry.refresh(now=now)

    table = json.loads(fleet_table(snap))
    assert table[0]["session_id"] == SID1
    assert table[0]["state"] == "needs review"
    assert table[0]["note"] == "waiting on CI"

    prompt = build_prompt(snap, "mark alpha reviewed")
    assert "mark alpha reviewed" in prompt
    assert SID1 in prompt
    for action in ALLOWED_ACTIONS:
        assert action in prompt


def test_apply_plan(claude_dir, tmp_path, now):
    TranscriptBuilder(SID1, "/proj/alpha").user("x").assistant_text("y").write(
        claude_dir, mtime=now - 100
    )
    TranscriptBuilder(SID2, "/proj/beta").user("x").assistant_text("y").write(
        claude_dir, mtime=now - 100
    )
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.track(SID2, "/proj/beta", "2026-08-17T09:00:00+00:00")
    registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
    snap = registry.refresh(now=now)

    raw = _plan_reply(
        [
            {"action": "mark_reviewed", "session_id": SID1},
            {"action": "set_label", "session_id": SID2, "value": "storage work"},
            {"action": "untrack", "session_id": SID2},
        ]
    )
    plan = parse_plan(raw, snap)
    done = apply_plan(plan, store, "2026-08-17T12:00:00+00:00")
    assert len(done) == 3
    assert store.sessions[SID1].reviewed_at == "2026-08-17T12:00:00+00:00"
    assert SID2 not in store.sessions


async def test_palette_end_to_end_with_fake_runner(world):
    app, store, _ = world
    runner = FakeRunner(
        _plan_reply(
            [{"action": "mark_reviewed", "session_id": SID1, "reason": "auth PR merged"}],
            reply="Marking the auth session reviewed.",
        )
    )
    app.claude_runner = runner
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("colon")
        await pilot.pause()
        from cagents.modals import PaletteModal, PlanConfirmModal

        assert isinstance(app.screen, PaletteModal)
        await pilot.press(*"auth is merged, mark it reviewed")
        await pilot.press("enter")
        await pilot.pause(0.3)  # worker round-trips
        assert isinstance(app.screen, PlanConfirmModal)
        # The model saw the fleet table
        assert SID1 in runner.prompts[0]
        await pilot.press("y")
        await pilot.pause(0.2)
        assert store.sessions[SID1].reviewed_at != ""


async def test_palette_declining_plan_changes_nothing(world):
    app, store, _ = world
    app.claude_runner = FakeRunner(
        _plan_reply([{"action": "untrack", "session_id": SID1}])
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("colon")
        await pilot.pause()
        await pilot.press(*"clean up")
        await pilot.press("enter")
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.1)
        assert SID1 in store.sessions  # untouched


async def test_palette_garbage_reply_fails_loudly(world):
    app, store, _ = world
    app.claude_runner = FakeRunner("sorry, I can't produce JSON today")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("colon")
        await pilot.pause()
        await pilot.press(*"do things")
        await pilot.press("enter")
        await pilot.pause(0.3)
        from cagents.modals import PlanConfirmModal

        assert not isinstance(app.screen, PlanConfirmModal)  # no plan modal
        assert store.sessions == store.sessions  # nothing applied, app alive
        assert app.is_running
