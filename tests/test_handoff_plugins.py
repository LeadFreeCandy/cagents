"""Tests for handoff, lineage navigation, and the plugin framework."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import SID1, SID2, SID3, TranscriptBuilder
from test_app import FakeTmux, render_text

from cagents.app import CagentsApp
from cagents.handoff import first_message, summary_prompt
from cagents.plugins import RESERVED_KEYS, PluginManager
from cagents.sessions import SessionRegistry, SessionState
from cagents.store import Store
from cagents.views import SessionList


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Original work").user("go").assistant_text(
        "Phase one complete."
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


# -------------------------------------------------------------- handoff ---


def test_handoff_prompts():
    prompt = summary_prompt("port it to rust")
    assert "port it to rust" in prompt and "ONLY the spec" in prompt
    message = first_message("THE SPEC", "port it to rust")
    assert message.index("THE SPEC") < message.index("Your task: port it to rust")


async def test_handoff_flow(world, monkeypatch):
    app, store, tmux = world
    sent = []
    tmux.send_text = lambda name, text, submit=True: sent.append((name, text))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class FakeHandoffRunner:
        def __init__(self):
            self.prompts = []

        def run(self, prompt):
            self.prompts.append(prompt)
            return "SPEC: we built X, next do Y."

    runner = FakeHandoffRunner()
    app._handoff_runner = lambda source_id: runner

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()
        await pilot.press(*"finish the API layer")
        await pilot.press("enter")
        await pilot.pause(0.5)  # summarize worker + spawn

        # summary was requested with the user's focus baked in
        assert "finish the API layer" in runner.prompts[0]
        # a NEW session started with --session-id (not a fork of the old)
        directory, args, new_id = tmux.created[-1]
        assert args == ["--session-id", new_id]
        # lineage recorded, labeled after the prompt
        child = store.sessions[new_id]
        assert child.parent_id == SID1 and child.relation == "handoff"
        assert child.label == "finish the API layer"
        # old session marked done (restorable with r)
        assert store.sessions[SID1].reviewed_at != ""
        # the spec + task became the first message
        assert sent and "SPEC: we built X" in sent[0][1]
        assert "Your task: finish the API layer" in sent[0][1]


async def test_handoff_empty_spec_aborts(world, monkeypatch):
    app, store, tmux = world
    app._handoff_runner = lambda sid: type("R", (), {"run": lambda self, p: "  "})()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("H")
        await pilot.pause()
        await pilot.press(*"x")
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert tmux.created == []  # nothing started
        assert store.sessions[SID1].reviewed_at == ""  # original untouched


# -------------------------------------------------------------- lineage ---


@pytest.fixture
def family(claude_dir: Path, tmp_path: Path, now: float):
    for sid, title in ((SID1, "Parent"), (SID2, "Fork A"), (SID3, "Fork B")):
        TranscriptBuilder(sid, "/proj/alpha").ai_title(title).user("go").assistant_text(
            "done"
        ).write(claude_dir, mtime=now - 500)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.track(SID2, "/proj/alpha", "2026-08-17T09:01:00+00:00",
                parent_id=SID1, relation="fork")
    store.track(SID3, "/proj/alpha", "2026-08-17T09:02:00+00:00",
                parent_id=SID1, relation="handoff")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store


def test_lineage_resolved_in_snapshot(family, now):
    app, store = family
    snap = app.registry.refresh(now=now)
    parent = snap.by_id(SID1)
    fork_a = snap.by_id(SID2)
    assert sorted(parent.child_ids) == sorted([SID2, SID3])
    assert fork_a.parent_id == SID1 and fork_a.relation == "fork"
    assert fork_a.sibling_ids == [SID3]


async def test_related_modal_lists_and_jumps(family):
    app, store = family
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        from test_app import select_session

        select_session(app, SID2)  # Fork A
        await pilot.pause()
        await pilot.press("asterisk")
        await pilot.pause()
        from cagents.modals import RelatedModal

        assert isinstance(app.screen, RelatedModal)
        listing = app.screen.query_one("#related-list")
        rows = "\n".join(
            render_text(listing.get_option_at_index(i).prompt)
            for i in range(listing.option_count)
        )
        assert "parent (fork)" in rows and "Parent" in rows
        assert "sibling" in rows and "Fork B" in rows
        # choose the parent -> selection jumps to it
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.selected_session_id == SID1


async def test_rows_show_lineage_markers(family):
    app, store = family
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        grouped = app.query_one("#grouped-list", SessionList)
        rows = "\n".join(
            render_text(grouped.get_option_at_index(i).prompt)
            for i in range(grouped.option_count)
        )
        assert "»2" in rows  # parent shows child count
        assert "↳" in rows  # children marked


# -------------------------------------------------------------- plugins ---


GOOD_PLUGIN = '''
CALLS = []

def run(api):
    api.notify("plugin ran")
    api.store.set_note(api.selected().session_id, "plugin was here")

def on_snapshot(api, snapshot):
    CALLS.append(len(snapshot.views))

PLUGIN = {
    "name": "test-plugin",
    "description": "test",
    "key": "ctrl+g",
    "run": run,
    "on_snapshot": on_snapshot,
}
'''


class TestPluginManager:
    def test_loads_and_hot_reloads(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
        manager = PluginManager(plugin_dir)
        assert manager.scan() == []
        record = manager.by_key("ctrl+g")
        assert record is not None and record.name == "test-plugin"
        # unchanged -> no reload; changed -> reload
        assert manager.scan() == []
        import os
        path = plugin_dir / "good.py"
        path.write_text(GOOD_PLUGIN.replace("ctrl+g", "ctrl+j"))
        os.utime(path, (time.time() + 5, time.time() + 5))
        manager.scan()
        assert manager.by_key("ctrl+g") is None
        assert manager.by_key("ctrl+j") is not None

    def test_broken_plugin_contained(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "bad.py").write_text("raise RuntimeError('boom')")
        (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
        manager = PluginManager(plugin_dir)
        errors = manager.scan()
        assert len(errors) == 1 and "bad.py" in errors[0]
        assert manager.by_key("ctrl+g") is not None  # good one still loads

    def test_reserved_keys_rejected(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "greedy.py").write_text(
            GOOD_PLUGIN.replace('"ctrl+g"', '"enter"')
        )
        manager = PluginManager(plugin_dir)
        errors = manager.scan()
        assert errors and "reserved" in errors[0]
        assert "enter" in RESERVED_KEYS

    def test_automation_scheduling(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "auto.py").write_text(
            "PLUGIN = {'name': 'auto', 'every': 300, 'tick': lambda api: None}"
        )
        manager = PluginManager(plugin_dir)
        manager.scan()
        now = time.time()
        assert len(manager.due_automations(now)) == 1
        assert manager.due_automations(now + 60) == []  # not due yet
        assert len(manager.due_automations(now + 301)) == 1


async def test_plugin_keybind_dispatch(world, tmp_path):
    app, store, _ = world
    plugin_dir = store.path.parent / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "noter.py").write_text(GOOD_PLUGIN)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause(0.2)
        assert store.sessions[SID1].note == "plugin was here"


async def test_add_plugin_creates_meta_session(world, monkeypatch):
    app, store, tmux = world
    sent = []
    tmux.send_text = lambda name, text, submit=True: sent.append(text)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("plus")
        await pilot.pause()
        await pilot.press(*"bind ctrl+y to copy the session id")
        await pilot.press("enter")
        await pilot.pause(0.4)
        directory, args, sid = tmux.created[-1]
        assert directory.endswith("/plugins")
        assert store.sessions[sid].label == "meta"
        # the guide + request went in as the first message
        assert sent and "PLUGIN dict" in sent[0]
        assert "bind ctrl+y to copy the session id" in sent[0]
