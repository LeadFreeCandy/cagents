"""Tests for sidecar mode and the compact rail rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SID1, TranscriptBuilder
from test_app import FakeTmux, widget_text

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry
from cagents.sidecar import Sidecar, nested_attach_command
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from cagents.views import SessionList


class FakeOuterTmux:
    """Records outer-tmux calls; simulates pane creation/liveness."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.panes = ["%0"]  # the cagents pane
        self._next = 1

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[0] == "split-window":
            pane = f"%{self._next}"
            self._next += 1
            self.panes.append(pane)
            return pane + "\n"
        if args[0] == "list-panes":
            return "\n".join(self.panes) + "\n"
        return ""


class TestSidecar:
    def test_enabled_requires_both_env_vars(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("CAGENTS_SIDECAR", raising=False)
        assert Sidecar.enabled() is False
        monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
        assert Sidecar.enabled() is False
        monkeypatch.setenv("CAGENTS_SIDECAR", "1")
        assert Sidecar.enabled() is True

    def test_first_open_splits_and_collapses(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.open("attach-cmd")
        kinds = [c[0] for c in outer.calls]
        assert kinds == ["split-window", "resize-pane", "select-pane"]
        assert sidecar.pane_id == "%1"
        resize = outer.calls[1]
        assert resize == ["resize-pane", "-t", "%0", "-x", "34"]
        assert outer.calls[2] == ["select-pane", "-t", "%1"]

    def test_second_open_respawns_same_pane(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.open("cmd-one")
        outer.calls.clear()
        sidecar.open("cmd-two")
        kinds = [c[0] for c in outer.calls]
        assert kinds == ["list-panes", "respawn-pane", "resize-pane", "select-pane"]
        respawn = outer.calls[1]
        assert respawn[-1] == "cmd-two" and "%1" in respawn

    def test_dead_pane_resplits(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.open("cmd")
        outer.panes.remove("%1")  # user closed the session pane
        outer.calls.clear()
        sidecar.open("cmd")
        kinds = [c[0] for c in outer.calls]
        assert "split-window" in kinds and "respawn-pane" not in kinds
        assert sidecar.pane_id == "%2"

    def test_nested_attach_command_quotes(self):
        cmd = nested_attach_command("claude", "my-repo")
        assert cmd == "env -u TMUX tmux -L claude attach-session -t '=my-repo'"
        assert "'\\''" in nested_attach_command("claude", "we'ird")


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user("go").write(
        claude_dir, mtime=now - 1
    )
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(name="alpha", created=now - 60, activity=now, attached=False,
                    pane_pid=1, pane_path="/proj/alpha")
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    return store, tmux, registry, claude_dir


async def test_attach_uses_sidecar_pane_not_suspend(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_attach()
        await pilot.pause()
        assert tmux.attached_to == []  # no terminal handoff
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert split[-1] == "env -u TMUX tmux -L claude attach-session -t '=alpha'"


async def test_compact_rail_rendering(world):
    store, tmux, registry, claude_dir = world
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(34, 40)) as pilot:
        await pilot.pause()
        assert app.compact is True
        assert app.query_one("#body").has_class("compact")
        assert str(app.query_one("#preview-pane").styles.display) == "none"
        # rows are dense: glyph + title, no full project path
        from test_app import render_text

        grouped = app.query_one("#grouped-list", SessionList)
        rows = "\n".join(
            render_text(grouped.get_option_at_index(i).prompt)
            for i in range(grouped.option_count)
        )
        assert "Alpha: fix auth" in rows
        assert "/proj/alpha" not in rows  # full path dropped in compact


async def test_compact_toggles_back_when_widened(world):
    store, tmux, registry, claude_dir = world
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(34, 40)) as pilot:
        await pilot.pause()
        assert app.compact is True
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert app.compact is False
        assert not app.query_one("#body").has_class("compact")
