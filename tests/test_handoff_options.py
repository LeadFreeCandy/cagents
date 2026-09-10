"""Provider/model handoffs and their selectors, without starting native agents."""
from __future__ import annotations

import json
import subprocess

import pytest
from textual.widgets import Input, Select

from conftest import FakeTmux, SID1
from cagents.app import CagentsApp
from cagents.claude_data import ParsedSession
from cagents.codex_rpc import CodexClient
from cagents.handoff import HandoffRequest, model_choices
from cagents.modals import HandoffModal
from cagents.sessions import SessionState, SessionView, Snapshot
from cagents.store import Store


@pytest.fixture
def world(tmp_path, monkeypatch):
    def make(provider="claude"):
        sid = ("codex:" if provider == "codex" else "") + SID1
        store = Store(tmp_path / "state.json")
        tracked = store.track(sid, "/project", "2026-09-10T10:00:00Z")
        path = tmp_path / "source.jsonl"
        path.write_text("original conversation history\n")
        parsed = ParsedSession(session_id=sid, path=path, cwd="/project", last_cwd="/project-worktree")
        view = SessionView(sid, tracked, parsed, SessionState.NEEDS_REVIEW, False)
        app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude",
                         codex_dir=tmp_path / "codex")
        monkeypatch.setattr(app, "refresh_data", lambda: None)
        monkeypatch.setattr(app, "_agent_bin", lambda name: name)
        monkeypatch.setattr(app, "_show_new_session", lambda _: None)
        return app, view
    return make


@pytest.mark.parametrize("source", ["claude", "codex"])
@pytest.mark.parametrize("destination", ["claude", "codex"])
@pytest.mark.parametrize("model", ["", "custom/model-v2"])
async def test_handoff_selects_successor_provider_and_model(world, monkeypatch, source, destination, model):
    app, view = world(source)
    calls, launches, messages, summaries = [], [], [], []
    child_id = "11234567-0123-4567-8901-012345678901"

    class Runner:
        def run(self, prompt):
            summaries.append(prompt)
            return "A spec written from the source."

    def runner(sid):
        assert sid == view.session_id
        return Runner()

    def create(self, cwd, **kwargs):
        calls.append((cwd, kwargs))
        return child_id

    monkeypatch.setattr(app, "_handoff_runner", runner)
    monkeypatch.setattr(CodexClient, "create", create)
    monkeypatch.setattr(app, "_spawn_session", lambda cwd, args, sid: launches.append((cwd, args, sid)) or "new-session")
    monkeypatch.setattr(app, "_send_prompt_later", lambda *args: messages.append(args))
    async with app.run_test(size=(120, 40)) as pilot:
        app.apply_snapshot(Snapshot([view]))
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HandoffModal)
        assert app.screen.query_one("#handoff-provider", Select).value == source
        app.screen.query_one("#handoff-provider", Select).value = destination
        await pilot.pause()
        if model:
            app.screen.query_one("#handoff-model", Select).value = "__custom__"
            await pilot.pause()
            app.screen.query_one("#handoff-custom-model", Input).value = model
        await pilot.press(*"Finish the tests", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert len(launches) == 1 and len(summaries) == 1
        cwd, args, sid = launches[0]
        child = app.store.sessions[sid]
        assert child.provider == destination
        assert (child.parent_id, child.relation) == (view.session_id, "handoff")
        assert cwd == child.project_dir == "/project-worktree"
        if destination == "codex":
            assert sid == "codex:" + child_id
            assert args[:2] == ["resume", child_id]
            assert calls == [(cwd, {"model": model} if model else {})]
        else:
            assert args[:2] == ["--session-id", sid]
            assert not calls
        if model:
            assert args[args.index("--model") + 1] == model
        else:
            assert "--model" not in args
        assert "A spec written from the source." in messages[0][1]
        assert "Your task: Finish the tests" in messages[0][1]
        assert app.store.sessions[view.session_id].reviewed_at
        assert view.parsed.path.read_text() == "original conversation history\n"
        assert not app._pending_handoffs


async def test_handoff_picker_keeps_models_per_provider_and_validates_custom(world):
    app, view = world()
    results = []
    async with app.run_test(size=(90, 32)) as pilot:
        modal = HandoffModal("Source", "claude", {
            "claude": [("Provider default", ""), ("Sonnet", "sonnet")],
            "codex": [("Provider default", ""), ("Local Codex", "local-codex")],
        })
        app.push_screen(modal, results.append)
        await pilot.pause()
        assert app.focused.id == "handoff-prompt"
        await pilot.press("enter")  # empty task leaves the dialog open
        assert app.screen is modal
        await pilot.press(*"Next phase", "shift+tab", "enter", "down", "enter")
        await pilot.pause()
        assert modal.query_one("#handoff-model", Select).value == "sonnet"
        provider = modal.query_one("#handoff-provider", Select)
        provider.value = "codex"
        await pilot.pause()
        assert modal.query_one("#handoff-model", Select).value == ""
        modal.query_one("#handoff-model", Select).value = "__custom__"
        await pilot.pause()
        modal.query_one("#handoff-prompt", Input).focus()
        await pilot.press("enter")  # custom ID is required
        assert app.screen is modal and app.focused.id == "handoff-custom-model"
        await pilot.press(*"org/model")
        provider.value = "claude"
        await pilot.pause()
        assert modal.query_one("#handoff-model", Select).value == "sonnet"
        assert not modal.query_one("#handoff-custom-model").display
        provider.value = "codex"
        await pilot.pause()
        assert modal.query_one("#handoff-custom-model", Input).value == "org/model"
        modal.query_one("#handoff-prompt", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert results == [HandoffRequest("Next phase", "codex", "org/model")]


@pytest.mark.parametrize("failure", ["missing-cli", "invalid-model", "start-failed", "cancel"])
async def test_handoff_failure_leaves_source_unreviewed(world, monkeypatch, failure):
    app, view = world()
    summaries = []

    class Runner:
        def run(self, prompt):
            summaries.append(prompt)
            return "Source spec"

    def fail(*args, **kwargs):
        raise RuntimeError("could not start")

    monkeypatch.setattr(app, "_handoff_runner", lambda _: Runner())
    monkeypatch.setattr(app, "_spawn_session", fail)
    if failure == "missing-cli":
        monkeypatch.setattr(app, "_agent_bin", lambda _: "")
    request = None if failure == "cancel" else HandoffRequest(
        "Next", "claude", "--bad-model" if failure == "invalid-model" else "sonnet")
    async with app.run_test(size=(120, 40)) as pilot:
        app.apply_snapshot(Snapshot([view]))
        await pilot.pause()
        app._handoff_confirmed(view.session_id, request)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.store.sessions) == 1
        assert not app.store.sessions[view.session_id].reviewed_at
        assert not app._pending_handoffs
        assert bool(summaries) == (failure == "start-failed")


def test_model_suggestions_use_local_catalog_and_history(tmp_path):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "codex-visible", "display_name": "Visible", "visibility": "list"},
        {"slug": "codex-hidden", "visibility": "hide"}, None, {},
    ]}))
    assert model_choices("codex", tmp_path, ["codex-visible", "org/custom", "", "<synthetic>"]) == [
        ("Provider default", ""), ("Visible", "codex-visible"), ("org/custom", "org/custom")]
    assert ("Sonnet", "sonnet") in model_choices("claude", tmp_path)
    assert "codex-visible" not in dict(model_choices("claude", tmp_path)).values()


@pytest.mark.parametrize("content", ["not json", "[]", '{"models":null}'])
def test_bad_model_catalog_still_allows_provider_default_and_observed_models(tmp_path, content):
    (tmp_path / "models_cache.json").write_text(content)
    assert model_choices("codex", tmp_path, ["my-model"]) == [("Provider default", ""), ("my-model", "my-model")]


@pytest.mark.parametrize("model", ["", "custom-model"])
def test_codex_thread_start_receives_the_selected_model(tmp_path, monkeypatch, model):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    client = CodexClient(tmp_path)
    calls = []
    monkeypatch.setattr(client, "call", lambda method, params: calls.append((method, params)) or {"thread": {"id": SID1}})
    assert client.create("/project", model=model) == SID1
    assert calls[0] == ("thread/start", {"cwd": "/project", **({"model": model} if model else {})})
    assert calls[1][0] == "thread/inject_items"
    assert calls[1][1]["threadId"] == SID1
    assert [item["role"] for item in calls[1][1]["items"]] == ["developer"]
    assert not any(method == "turn/start" for method, _ in calls)
