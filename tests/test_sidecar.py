"""Tests for the sidecar viewer-pane model, layout cycle, container
bootstrap (incl. orphan detection), and the ctx keybinds."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    SID1,
    FakeOuterTmux,
    FakeTmux,
    TranscriptBuilder,
    select_session,
    ts_ago,
)

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry
from cagents.sidecar import (
    Sidecar,
    container_setup_commands,
    ctx_bind_commands,
    left_capture_commands,
    nested_attach_command,
    preview_command,
    self_command,
    should_bootstrap,
)
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from cagents.views import SessionList


class TestSidecarPane:
    def test_enabled_inside_any_tmux_unless_opted_out(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("CAGENTS_SIDECAR", raising=False)
        assert Sidecar.enabled() is False
        monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
        assert Sidecar.enabled() is True
        monkeypatch.setenv("CAGENTS_SIDECAR", "0")
        assert Sidecar.enabled() is False

    def test_first_show_splits_without_stealing_focus(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.show_viewer("attach-cmd")
        kinds = [c[0] for c in outer.calls]
        assert "split-window" in kinds
        assert "select-pane" not in kinds  # browsing never steals focus
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert "-d" in split and split[-1] == "attach-cmd"
        assert sidecar.pane_id == "%1"

    def test_same_command_is_a_noop(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.show_viewer("cmd")
        count = len(outer.calls)
        sidecar.show_viewer("cmd")  # highlight didn't really change target
        assert len(outer.calls) == count + 1  # only the liveness check ran

    def test_new_command_respawns_same_pane(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.show_viewer("cmd-one")
        outer.calls.clear()
        sidecar.show_viewer("cmd-two")
        respawn = next(c for c in outer.calls if c[0] == "respawn-pane")
        assert "-k" in respawn and respawn[-1] == "cmd-two" and "%1" in respawn

    def test_dead_pane_resplits(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.show_viewer("cmd")
        outer.panes.remove("%1")
        outer.calls.clear()
        sidecar.show_viewer("cmd-two")
        assert any(c[0] == "split-window" for c in outer.calls)
        assert sidecar.pane_id == "%2"

    def test_focus_and_hide(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.show_viewer("cmd")
        sidecar.focus_session()
        assert ["select-pane", "-t", "%1"] in outer.calls
        sidecar.focus_rail()
        assert ["select-pane", "-t", "%0"] in outer.calls
        outer.calls.clear()
        sidecar.hide_rail()
        assert ["resize-pane", "-Z", "-t", "%1"] in outer.calls  # zoom = hidden rail

    def test_split_shell_below_viewer(self):
        outer = FakeOuterTmux()
        sidecar = Sidecar(runner=outer, own_pane="%0")
        sidecar.show_viewer("cmd")
        outer.calls.clear()
        sidecar.split_shell("/some/dir")
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert "-v" in split and "/some/dir" in split and "%1" in split


class TestCommands:
    def test_nested_attach_command_quotes_and_socket(self):
        cmd = nested_attach_command("claude", "my-repo")
        assert cmd == "env -u TMUX tmux -L claude attach-session -t '=my-repo'"
        assert "-L cagents-sessions" in nested_attach_command("cagents-sessions", "x")
        assert "'\\''" in nested_attach_command("claude", "we'ird")

    def test_preview_command_carries_store_and_dir(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/cagents"])
        cmd = preview_command(SID1, "/data/state.json", "/claude/dir")
        assert cmd.startswith("/opt/venv/bin/cagents --preview-session")
        assert SID1 in cmd and "/data/state.json" in cmd and "/claude/dir" in cmd

    def test_self_command_preserves_launch_cwd(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/cagents"])
        monkeypatch.setenv("CAGENTS_LAUNCH_CWD", "/where/i/was")
        cmd = self_command(["--store", "/tmp/s.json"])
        assert cmd.startswith("CAGENTS_SIDECAR=1 CAGENTS_LAUNCH_CWD=/where/i/was ")
        assert "/opt/venv/bin/cagents --store /tmp/s.json" in cmd

    def test_container_setup_binds_no_letter_or_escape_keys(self):
        flat = [" ".join(c) for c in container_setup_commands()]
        assert not any(" Escape " in f" {c} " for c in flat)  # Esc stays Claude's
        # No key bindings in static setup — the ← cycle comes from
        # left_capture_commands (toggleable); C-s/C-d from ctx binds.
        assert not any(c.startswith("bind") for c in flat)
        assert any("after-select-pane" in c for c in flat)
        assert any("status-right" in c for c in flat)  # the statusline

    def test_arrow_bindings_are_a_size_control(self):
        on = left_capture_commands(True)
        left = " ".join(on[0])
        right = " ".join(on[1])
        # ← shrinks: HIDDEN -> SMALL (unzoom) or SMALL -> WIDE (focus rail)
        assert left.startswith("bind -n Left")
        assert "send-keys Left" in left  # rail-focused: passes to the app
        assert "window_zoomed_flag" in left
        assert "select-pane -t :.0" in left
        # → grows: SMALL -> HIDDEN (zoom); at HIDDEN it passes to Claude
        assert right.startswith("bind -n Right")
        assert "send-keys Right" in right
        assert "resize-pane -Z -t :.1" in right
        assert left_capture_commands(False) == [
            ["unbind", "-n", "Left"], ["unbind", "-n", "Right"],
        ]

    def test_ctx_bind_commands(self):
        commands = ctx_bind_commands("/venv/bin/cagents-ctx", "/data/context.json")
        flat = [" ".join(c) for c in commands]
        assert any(c.startswith("bind -n C-s run-shell") and "shell" in c for c in flat)
        assert any(c.startswith("bind -n C-d run-shell") and "diff" in c for c in flat)

    def test_should_bootstrap_only_bare_terminal(self):
        assert should_bootstrap({}, stdout_is_tty=True, fullscreen_flag=False) is True
        assert should_bootstrap({"TMUX": "x"}, True, False) is False
        assert should_bootstrap({"CAGENTS_SIDECAR": "1"}, True, False) is False
        assert should_bootstrap({}, True, True) is False
        assert should_bootstrap({}, False, False) is False


class TestOrphanDetection:
    def _healthy(self, panes_output: str, returncode: int = 0) -> bool:
        import subprocess
        from unittest import mock

        from cagents.sidecar import _container_is_healthy

        completed = subprocess.CompletedProcess([], returncode, stdout=panes_output, stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            return _container_is_healthy(["tmux", "-L", "x"])

    def test_healthy_when_pane0_runs_cagents(self):
        assert self._healthy("0\x1fpython3.10\n1\x1ftmux\n") is True
        assert self._healthy("0\x1fcagents\n") is True

    def test_orphan_when_pane0_is_a_session(self):
        # the app died; a claude pane got renumbered into slot 0
        assert self._healthy("0\x1ftmux\n") is False
        assert self._healthy("0\x1fnode\n") is False
        assert self._healthy("", returncode=1) is False


# ------------------------------------------------------------- app + pane --


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user(
        "go", ts=ts_ago(1)
    ).write(claude_dir, mtime=now - 1)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(name="alpha", created=now - 60, activity=now, attached=False,
                    pane_pid=1, pane_path="/proj/alpha", socket="claude")
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    return store, tmux, registry, claude_dir


async def test_viewer_follows_highlight_after_debounce(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)  # refresh + debounce elapse
        split = next((c for c in outer.calls if c[0] == "split-window"), None)
        assert split is not None
        # live session on the claude socket -> a real attach of it
        assert split[-1] == "env -u TMUX tmux -L claude attach-session -t '=alpha'"
        # browsing never selected the pane
        assert not any(c[0] == "select-pane" for c in outer.calls)


async def test_enter_focuses_the_same_pane(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        respawns_before = sum(1 for c in outer.calls if c[0] in ("split-window", "respawn-pane"))
        app.action_attach()
        await pilot.pause()
        # Enter = focus only; the viewer already shows the session
        respawns_after = sum(1 for c in outer.calls if c[0] in ("split-window", "respawn-pane"))
        assert respawns_after == respawns_before
        assert ["select-pane", "-t", "%1"] in outer.calls
        assert tmux.attached_to == []  # no fullscreen handoff happened


async def test_dead_session_gets_transcript_preview(world, claude_dir, now, tmp_path):
    store, tmux, registry, _ = world
    sid_dead = "99999999-9999-9999-9999-999999999999"
    TranscriptBuilder(sid_dead, "/proj/beta").ai_title("Old work").user("x").assistant_text(
        "finished"
    ).write(claude_dir, mtime=now - 5000)
    store.track(sid_dead, "/proj/beta", "2026-08-18T07:00:00+00:00")
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, sid_dead)
        await pilot.pause(0.5)  # debounce
        shown = [c[-1] for c in outer.calls if c[0] in ("split-window", "respawn-pane")]
        assert any("--preview-session" in cmd and sid_dead in cmd for cmd in shown)


async def test_internal_preview_hidden_in_sidecar_mode(world):
    store, tmux, registry, claude_dir = world
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=FakeOuterTmux(), own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#body").has_class("sidecar")
        assert str(app.query_one("#preview-pane").styles.display) == "none"


async def test_right_in_list_grows_session(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("right")  # rail focused: WIDE -> SMALL (focus session)
        await pilot.pause()
        assert ["select-pane", "-t", "%1"] in outer.calls


async def test_arrows_in_kanban_move_columns_not_layout(world):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("3")
        await pilot.pause()
        outer.calls.clear()
        await pilot.press("right")
        await pilot.pause()
        assert ["select-pane", "-t", "%1"] not in outer.calls  # kanban consumed it


async def test_compact_rail_rendering(world):
    store, tmux, registry, claude_dir = world
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(34, 40)) as pilot:
        await pilot.pause()
        assert app.compact is True
        assert app.query_one("#body").has_class("compact")
        from conftest import render_text

        grouped = app.query_one("#grouped-list", SessionList)
        rows = "\n".join(
            render_text(grouped.get_option_at_index(i).prompt)
            for i in range(grouped.option_count)
        )
        assert "Alpha: fix auth" in rows
        assert "/proj/alpha" not in rows  # full path dropped in compact


async def test_context_file_follows_selection(world, tmp_path):
    store, tmux, registry, claude_dir = world
    outer = FakeOuterTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0"),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        from cagents.ctx import read_context

        context = read_context(store.path.parent / "context.json")
        assert context.get("dir") == "/proj/alpha"
        assert context.get("session_id") == SID1
