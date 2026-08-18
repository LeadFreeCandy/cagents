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
    def test_enabled_inside_any_tmux_unless_opted_out(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("CAGENTS_SIDECAR", raising=False)
        assert Sidecar.enabled() is False  # no tmux, no panes to split
        monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
        assert Sidecar.enabled() is True  # user's own tmux: split in place
        monkeypatch.setenv("CAGENTS_SIDECAR", "1")
        assert Sidecar.enabled() is True  # the container
        monkeypatch.setenv("CAGENTS_SIDECAR", "0")
        assert Sidecar.enabled() is False  # --fullscreen opt-out

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

    def test_nested_attach_command_read_only(self):
        cmd = nested_attach_command("claude", "my-repo", read_only=True)
        assert cmd == "env -u TMUX tmux -L claude attach-session -r -t '=my-repo'"

    def test_preview_spawns_without_moving_focus(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.preview("attach-cmd")
        kinds = [c[0] for c in outer.calls]
        assert kinds == ["split-window"]  # no resize-pane, no select-pane
        assert sidecar.pane_id == "%1"

    def test_second_preview_respawns_same_pane_without_moving_focus(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.preview("cmd-one")
        outer.calls.clear()
        sidecar.preview("cmd-two")
        kinds = [c[0] for c in outer.calls]
        assert kinds == ["list-panes", "respawn-pane"]


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


async def test_sidecar_active_hides_internal_preview_pane(world):
    """Two panes, never three: when the sidecar is doing the live-preview
    job, cagents' own internal fallback text preview must not also be
    showing next to the list — that was rendering as a third screen."""
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#preview-pane").styles.display) == "none"


async def test_no_sidecar_still_shows_the_internal_preview_pane(world):
    """Without a sidecar (no tmux at all), the internal text preview is
    the only preview there is — it must stay visible."""
    store, tmux, registry, claude_dir = world
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir, sidecar=None)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#preview-pane").styles.display) != "none"


async def test_sidebar_setting_off_shows_internal_preview_even_with_sidecar(world):
    """Sidecar object present but the user turned the 'sidebar' setting
    off: no live pane will ever be used, so the fallback must show."""
    store, tmux, registry, claude_dir = world
    store.set_setting("sidebar", False)
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#preview-pane").styles.display) != "none"


async def test_attach_uses_sidecar_pane_not_suspend(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # The live selection already put a read-only preview in the pane.
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert split[-1] == "env -u TMUX tmux -L claude attach-session -r -t '=alpha'"
        app.action_attach()
        await pilot.pause()
        assert tmux.attached_to == []  # no terminal handoff
        respawn = next(c for c in outer.calls if c[0] == "respawn-pane")
        assert respawn[-1] == "env -u TMUX tmux -L claude attach-session -t '=alpha'"


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


class TestContainerBootstrap:
    def test_should_bootstrap_only_bare_terminal(self):
        from cagents.sidecar import should_bootstrap

        assert should_bootstrap({}, stdout_is_tty=True, fullscreen_flag=False) is True
        # already inside tmux -> sidecar splits in place instead
        assert should_bootstrap({"TMUX": "x"}, True, False) is False
        # already the container pane
        assert should_bootstrap({"CAGENTS_SIDECAR": "1"}, True, False) is False
        # explicit classic mode
        assert should_bootstrap({}, True, True) is False
        # not a terminal (tests, pipes)
        assert should_bootstrap({}, False, False) is False

    def test_self_command_quotes_and_marks_sidecar(self, monkeypatch):
        import sys

        from cagents.sidecar import self_command

        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/cagents"])
        cmd = self_command(["--store", "/tmp/my store.json"])
        assert cmd.startswith("CAGENTS_SIDECAR=1 /opt/venv/bin/cagents")
        assert "'/tmp/my store.json'" in cmd

    def test_self_command_module_invocation(self, monkeypatch):
        import sys

        from cagents.sidecar import self_command

        monkeypatch.setattr(sys, "argv", ["/x/cagents/__main__.py"])
        cmd = self_command([])
        assert "-m cagents" in cmd

    def test_container_setup_never_binds_escape(self):
        from cagents.sidecar import container_setup_commands

        commands = container_setup_commands()
        flat = [" ".join(c) for c in commands]
        assert not any("Escape" in c for c in flat)  # Esc stays Claude's
        assert any("M-q" in c for c in flat)
        assert any("C-\\" in c for c in flat)  # works without Meta config
        assert any("after-select-pane" in c for c in flat)


async def test_expand_rail_key(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(34, 40)) as pilot:
        await pilot.pause()
        await pilot.press("equals_sign")
        await pilot.pause()
        assert ["resize-pane", "-t", "%0", "-x", "50%"] in outer.calls
