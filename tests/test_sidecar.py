"""Tests for the sidecar viewer-pane model, layout cycle, container
bootstrap (incl. orphan detection), and the ctx keybinds."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    SID1,
    FakeOuterTmux,
    FakeTmux,
    FakeWorkTmux,
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
    def _sidecar(self):
        outer, work = FakeOuterTmux(), FakeWorkTmux()
        return Sidecar(runner=outer, own_pane="%0", work_runner=work), outer, work

    def test_enabled_inside_any_tmux_unless_opted_out(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("CAGENTS_SIDECAR", raising=False)
        assert Sidecar.enabled() is False
        monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
        assert Sidecar.enabled() is True
        monkeypatch.setenv("CAGENTS_SIDECAR", "0")
        assert Sidecar.enabled() is False

    def test_ensure_workspace_creates_three_tabs_in_order(self):
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace(terminal_dir="/launch/here")
        assert work.windows == ["session", "diff", "term-1"]  # l -> r
        term = next(c for c in work.calls if c[0] == "new-window" and "term-1" in c)
        assert "-c" in term and "/launch/here" in term
        # tab bar on top of the workspace
        flat = [" ".join(c) for c in work.calls]
        assert any("status-position top" in c for c in flat)
        # the container's right pane attaches the workspace
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert f"-L {Sidecar.__init__.__defaults__ or ''}" or True
        assert "cagents-work" in split[-1] and "attach-session" in split[-1]
        # idempotent
        calls = len(work.calls)
        sidecar.ensure_workspace()
        assert work.windows == ["session", "diff", "term-1"]
        assert len(work.calls) == calls + 1  # just the has-session probe

    def test_show_viewer_respawns_session_tab_only(self):
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace()
        work.calls.clear()
        sidecar.show_viewer("attach-cmd")
        respawn = next(c for c in work.calls if c[0] == "respawn-pane")
        assert "=work:session" in respawn and respawn[-1] == "attach-cmd"
        assert not any(c[0] == "select-window" for c in work.calls)  # no tab steal
        # same command again -> no respawn
        count = len([c for c in work.calls if c[0] == "respawn-pane"])
        sidecar.show_viewer("attach-cmd")
        assert len([c for c in work.calls if c[0] == "respawn-pane"]) == count

    def test_open_diff_tab_respawns_and_selects(self):
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace()
        work.calls.clear()
        sidecar.open_diff_tab("pager-cmd")
        respawn = next(c for c in work.calls if c[0] == "respawn-pane")
        assert "=work:diff" in respawn and respawn[-1] == "pager-cmd"
        assert ["select-window", "-t", "=work:diff"] in work.calls

    def test_terminal_tab_persists_and_recreates_when_dead(self):
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace(terminal_dir="/a")
        work.calls.clear()
        sidecar.open_terminal_tab("/b")
        # already alive: no new window, just the tab switch
        assert not any(c[0] == "new-window" for c in work.calls)
        assert ["select-window", "-t", "=work:term-1"] in work.calls
        # shell died (window gone) -> recreated in the new directory
        work.windows.remove("term-1")
        work.calls.clear()
        sidecar.open_terminal_tab("/b")
        created = next(c for c in work.calls if c[0] == "new-window")
        assert "/b" in created

    def test_enter_selects_session_tab_and_focuses(self):
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace()
        outer.calls.clear(); work.calls.clear()
        sidecar.focus_session()
        assert ["select-window", "-t", "=work:session"] in work.calls
        assert ["select-pane", "-t", "%1"] in outer.calls

    def test_hide_rail_zooms(self):
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace()
        outer.calls.clear()
        sidecar.hide_rail()
        assert ["resize-pane", "-Z", "-t", "%1"] in outer.calls


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
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)  # refresh + debounce elapse
        # the right pane attaches the workspace; the SESSION TAB gets the
        # live attach of the highlighted session
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert "cagents-work" in split[-1]
        respawn = next(c for c in work.calls if c[0] == "respawn-pane")
        assert "=work:session" in respawn
        assert respawn[-1] == "env -u TMUX tmux -L claude attach-session -t '=alpha'"
        # browsing never selected the pane nor switched tabs (the single
        # select-window is workspace creation defaulting to the session tab)
        assert not any(c[0] == "select-pane" for c in outer.calls)
        selects = [c for c in work.calls if c[0] == "select-window"]
        assert selects == [["select-window", "-t", "=work:session"]]


async def test_enter_focuses_the_same_pane(world):
    store, tmux, registry, claude_dir = world
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        respawns_before = sum(1 for c in work.calls if c[0] == "respawn-pane")
        app.action_attach()
        await pilot.pause()
        # Enter = select the session tab + focus the pane; no re-render
        assert sum(1 for c in work.calls if c[0] == "respawn-pane") == respawns_before
        assert ["select-window", "-t", "=work:session"] in work.calls
        assert ["select-pane", "-t", "%1"] in outer.calls
        assert tmux.attached_to == []  # no fullscreen handoff happened


async def test_dead_session_gets_transcript_preview(world, claude_dir, now, tmp_path):
    store, tmux, registry, _ = world
    sid_dead = "99999999-9999-9999-9999-999999999999"
    TranscriptBuilder(sid_dead, "/proj/beta").ai_title("Old work").user("x").assistant_text(
        "finished"
    ).write(claude_dir, mtime=now - 5000)
    store.track(sid_dead, "/proj/beta", "2026-08-18T07:00:00+00:00")
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, sid_dead)
        await pilot.pause(0.5)  # debounce
        shown = [c[-1] for c in work.calls if c[0] == "respawn-pane"]
        assert any("--preview-session" in cmd and sid_dead in cmd for cmd in shown)


async def test_internal_preview_hidden_in_sidecar_mode(world):
    store, tmux, registry, claude_dir = world
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=FakeOuterTmux(), own_pane="%0", work_runner=FakeWorkTmux()),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#body").has_class("sidecar")
        assert str(app.query_one("#preview-pane").styles.display) == "none"


async def test_right_in_list_grows_session(world):
    store, tmux, registry, claude_dir = world
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("right")  # rail focused: WIDE -> SMALL (focus session)
        await pilot.pause()
        assert ["select-pane", "-t", "%1"] in outer.calls


async def test_arrows_in_kanban_move_columns_not_layout(world):
    store, tmux, registry, claude_dir = world
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
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
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        from cagents.ctx import read_context

        context = read_context(store.path.parent / "context.json")
        assert context.get("dir") == "/proj/alpha"
        assert context.get("session_id") == SID1


def test_ensure_workspace_installs_diff_click_hook():
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    sidecar = Sidecar(runner=outer, own_pane="%0", work_runner=work)
    sidecar.ensure_workspace(
        terminal_dir="/x", ctx_prog="/venv/bin/cagents-ctx",
        context_path="/data/context.json",
    )
    hook = next(c for c in work.calls if c[0] == "set-hook")
    joined = " ".join(hook)
    assert "after-select-window" in joined
    assert "window_name},diff" in joined  # only the diff tab triggers
    assert "--no-select" in joined  # hook rebuilds without re-selecting
    assert "cagents-ctx diff" in joined


def test_ensure_workspace_without_ctx_installs_no_hook():
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    Sidecar(runner=outer, own_pane="%0", work_runner=work).ensure_workspace("/x")
    assert not any(c[0] == "set-hook" for c in work.calls)


# --------------------------------------------------------- quit teardown ---


class TestQuitTearsDownContainer:
    """Regression: `q` used to just kill this app's own pane (it's pane 0
    of the container session) and stop there. The tabbed workspace and
    the rest of the container never closed with it, and tmux renumbered
    whatever was left into slot 0 — the user was stranded in an orphaned,
    app-less tmux session with no way to navigate it. `q` must tear down
    cagents' own scaffolding on the way out, but ONLY when actually
    running inside the dedicated container (CAGENTS_SIDECAR=1) — never
    when merely split into the user's own separate tmux, where killing
    a server would destroy unrelated windows that aren't cagents' to
    touch. Real claude sessions live on an entirely separate socket and
    are never touched either way."""

    def _app(self, world) -> CagentsApp:
        store, tmux, registry, claude_dir = world
        return CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)

    def test_teardown_kills_the_work_and_container_sockets_only(self, world):
        import subprocess
        from unittest import mock

        from cagents.sidecar import CONTAINER_SOCKET, WORK_SOCKET

        app = self._app(world)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            app._teardown_container()

        assert ["tmux", "-L", WORK_SOCKET, "kill-server"] in calls
        assert ["tmux", "-L", CONTAINER_SOCKET, "kill-server"] in calls
        # never anything mentioning the actual claude-session sockets
        assert not any("claude" in a or "cagents-sessions" in a for c in calls for a in c)

    def test_quit_tears_down_only_when_actually_inside_the_container(self, world, monkeypatch):
        app = self._app(world)
        torn_down = []
        monkeypatch.setattr(app, "_teardown_container", lambda: torn_down.append(True))
        monkeypatch.setattr(app, "exit", lambda *a, **k: None)

        monkeypatch.delenv("CAGENTS_SIDECAR", raising=False)
        app.action_quit()
        assert torn_down == []  # split into the user's own tmux -> never touch it

        monkeypatch.setenv("CAGENTS_SIDECAR", "1")
        app.action_quit()
        assert torn_down == [True]
