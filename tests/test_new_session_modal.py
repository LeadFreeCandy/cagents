"""Tests for the new-session directory picker: launch-cwd default, the
tab-completing DirectoryInput, and the "terminal passthrough" shell picker."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from conftest import SID1, TranscriptBuilder
from test_app import FakeTmux, select_session

from cagents.app import CagentsApp
from cagents.modals import DirectoryInput
from cagents.sessions import SessionRegistry
from cagents.store import Store
from textual.app import App, ComposeResult


class _HarnessApp(App):
    """Minimal host so DirectoryInput can be exercised through a real
    mount/focus/key-event cycle without pulling in all of CagentsApp."""

    def __init__(self, value: str = "") -> None:
        super().__init__()
        self._initial_value = value

    def compose(self) -> ComposeResult:
        yield DirectoryInput(value=self._initial_value, id="dir")

    def on_mount(self) -> None:
        self.query_one("#dir", DirectoryInput).focus()


async def test_tab_completes_a_unique_prefix(tmp_path: Path):
    (tmp_path / "project-alpha").mkdir()
    (tmp_path / "project-beta").mkdir()
    (tmp_path / "not-a-dir.txt").write_text("x")
    app = _HarnessApp(value=str(tmp_path / "project-a"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        value = app.query_one("#dir", DirectoryInput).value
        assert value == str(tmp_path / "project-alpha") + "/"


async def test_tab_cycles_through_multiple_matches(tmp_path: Path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    app = _HarnessApp(value=str(tmp_path / "proj-"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        first = app.query_one("#dir", DirectoryInput).value
        await pilot.press("tab")
        await pilot.pause()
        second = app.query_one("#dir", DirectoryInput).value
        assert {first, second} == {str(tmp_path / "proj-a") + "/", str(tmp_path / "proj-b") + "/"}
        assert first != second

        # a third tab cycles back to the first
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#dir", DirectoryInput).value == first


async def test_dotdirs_hidden_unless_prefix_is_dotted(tmp_path: Path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible").mkdir()
    app = _HarnessApp(value=str(tmp_path) + "/")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#dir", DirectoryInput).value == str(tmp_path / "visible") + "/"


async def test_typing_after_completion_resets_the_cycle(tmp_path: Path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    app = _HarnessApp(value=str(tmp_path / "proj-"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        dir_input = app.query_one("#dir", DirectoryInput)
        assert dir_input._completions  # cached from the tab press
        dir_input.value = str(tmp_path / "proj-")  # simulate the user typing again
        await pilot.pause()
        assert dir_input._completions == []


async def test_no_matches_leaves_value_unchanged(tmp_path: Path):
    app = _HarnessApp(value=str(tmp_path / "nothing-starts-like-this"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#dir", DirectoryInput).value == str(
            tmp_path / "nothing-starts-like-this"
        )


@pytest.fixture
def cagents_world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Fix auth").user("go").assistant_text(
        "done"
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


async def test_new_session_defaults_to_launch_cwd_not_selected_session(cagents_world, monkeypatch):
    """However a different session's project dir differs from wherever
    cagents was actually launched from, a brand-new session must default
    to the launch directory — not whatever happens to be selected."""
    app, store, tmux = cagents_world
    monkeypatch.setattr(app, "_launch_cwd", "/somewhere/i/launched/cagents/from")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # a session IS selected
        await pilot.pause()
        app.action_new_session()
        await pilot.pause()
        from cagents.modals import NewSessionModal

        assert isinstance(app.screen, NewSessionModal)
        assert app.screen.initial_dir == "/somewhere/i/launched/cagents/from"


async def test_pick_directory_via_shell_uses_the_shells_final_pwd(cagents_world, tmp_path: Path):
    app, store, tmux = cagents_world
    picked_dir = tmp_path / "wherever-i-cd-ed-to"
    picked_dir.mkdir()

    def fake_run(args, **kwargs):
        script = args[2]
        match = re.search(r"trap 'pwd > (\S+)' EXIT", script)
        assert match, f"expected a pwd EXIT trap in: {script!r}"
        Path(match.group(1)).write_text(f"{picked_dir}\n")

    original_run = subprocess.run
    subprocess.run = fake_run  # local `import subprocess` in app.py shares this module object
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            result = app.pick_directory_via_shell(str(tmp_path))
            assert result == str(picked_dir)
    finally:
        subprocess.run = original_run


async def test_pick_directory_via_shell_falls_back_to_start_dir_if_nothing_written(
    cagents_world, tmp_path: Path
):
    app, store, tmux = cagents_world

    def fake_run(args, **kwargs):
        pass  # simulate the shell never writing anything (e.g. killed)

    original_run = subprocess.run
    subprocess.run = fake_run
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            result = app.pick_directory_via_shell(str(tmp_path))
            assert result == str(tmp_path)
    finally:
        subprocess.run = original_run
