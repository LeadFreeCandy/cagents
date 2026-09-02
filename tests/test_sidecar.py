"""Tests for the sidecar viewer-pane model, layout cycle, container
bootstrap (incl. orphan detection), and the ctx keybinds."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    SID1,
    SID2,
    FakeOuterTmux,
    FakeTmux,
    FakeWorkTmux,
    TranscriptBuilder,
    init_git_repo,
    select_session,
    ts_ago,
)

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry
from cagents.sidecar import (
    Sidecar,
    container_setup_commands,
    ctx_bind_commands,
    arrow_capture_commands,
    nested_attach_command,
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
        flat = [" ".join(c) for c in work.calls]
        # tab bar on top of the workspace
        assert any("status-position top" in c for c in flat)
        # the container's right pane attaches the workspace
        split = next(c for c in outer.calls if c[0] == "split-window")
        assert "cagents-work" in split[-1] and "attach-session" in split[-1]
        # idempotent: a second call creates no duplicate tabs, even though
        # it re-applies options/hooks every time on purpose (see the
        # persists-across-restart tests below)
        calls_before = len(work.calls)
        sidecar.ensure_workspace()
        assert work.windows == ["session", "diff", "term-1"]
        assert not any(c[0] == "new-window" for c in work.calls[calls_before:])

    def test_hooks_apply_even_to_a_workspace_that_already_existed(self):
        # Real bug this fixes: hooks/options used to be set only inside the
        # "session doesn't exist yet" branch, so a work session that
        # survived a cagents restart (or predates a version that added a
        # hook) never got it applied. Must reach an already-existing
        # session too, not just a freshly created one.
        sidecar, outer, work = self._sidecar()
        work.exists = True
        work.windows = ["session", "diff", "term-1"]
        sidecar.ensure_workspace(ctx_prog="/bin/cagents-ctx", context_path="/tmp/ctx.json")
        flat = [" ".join(c) for c in work.calls]
        assert any("after-select-window" in c and "diff" in c for c in flat)
        assert any("after-select-window" in c and "term-1" in c for c in flat)
        assert any("mouse on" in c for c in flat)

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

    def test_open_terminal_tab_respawns_only_when_the_target_changes(self):
        # Each session passes its OWN command (a nested attach into that
        # session's terminal window, or a plain shell) — the tab must
        # actually switch content when the target session changes, not
        # just re-select an unrelated shell that happened to be there.
        sidecar, outer, work = self._sidecar()
        sidecar.ensure_workspace(terminal_dir="/a")
        work.calls.clear()
        sidecar.open_terminal_tab("attach-session-a")
        respawn = next(c for c in work.calls if c[0] == "respawn-pane")
        assert "=work:term-1" in respawn and respawn[-1] == "attach-session-a"
        assert ["select-window", "-t", "=work:term-1"] in work.calls
        # same target again -> no respawn (don't kill work already running)
        count = len([c for c in work.calls if c[0] == "respawn-pane"])
        sidecar.open_terminal_tab("attach-session-a")
        assert len([c for c in work.calls if c[0] == "respawn-pane"]) == count
        # a different session's terminal -> respawns to the new target
        sidecar.open_terminal_tab("attach-session-b")
        respawn = [c for c in work.calls if c[0] == "respawn-pane"][-1]
        assert respawn[-1] == "attach-session-b"

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
        # arrow_capture_commands (toggleable); C-t/C-d from ctx binds.
        assert not any(c.startswith("bind") for c in flat)
        assert any("after-select-pane" in c for c in flat)
        assert any("status-right" in c for c in flat)  # the statusline

    def test_arrow_bindings_are_a_size_control(self):
        on = arrow_capture_commands()
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
        assert arrow_capture_commands(bare=False)[:2] == [
            ["unbind", "-n", "Left"], ["unbind", "-n", "Right"],
        ]

    def test_a_captured_arrow_is_the_size_control_unconditionally_by_default(self):
        """The default takes the bare pair outright — no probe, no screen
        read, Claude never sees them. Everything else is opt-in."""
        for command in arrow_capture_commands():
            assert "run-shell" not in " ".join(command)
            assert "cursor_x" not in " ".join(command)

    def test_ctrl_arrows_are_a_second_capture_off_by_default(self):
        """⌃←/⌃→ are Claude's word jump, so capturing them is a setting.
        Captured, they drive the same three states as the bare pair and
        pass through to the pane the same way."""
        default = {c[2] for c in arrow_capture_commands() if c[0] == "unbind"}
        assert default == {"C-Left", "C-Right"}
        with_ctrl = {c[2]: " ".join(c)
                     for c in arrow_capture_commands(ctrl=True) if c[0] == "bind"}
        assert {"Left", "Right", "C-Left", "C-Right"} <= set(with_ctrl)
        assert "send-keys C-Left" in with_ctrl["C-Left"]      # rail: to the app
        assert "select-pane -t :.0" in with_ctrl["C-Left"]    # session: resize
        assert "resize-pane -Z -t :.1" in with_ctrl["C-Right"]

    def test_the_composer_check_gates_the_bare_pair_only(self):
        """The bare arrows are the ones you type with, so the check hands
        those back to Claude while there is text in the composer. A ⌃
        arrow you have deliberately captured always resizes — that is what
        leaves you a working layout key mid-sentence."""
        gated = {c[2]: " ".join(c)
                 for c in arrow_capture_commands(ctrl=True, probe="/p/probe")
                 if c[0] == "bind"}
        for key in ("Left", "Right"):
            assert f"/p/probe {key} " in gated[key]
        for key in ("C-Left", "C-Right", "M-Left", "S-Right"):
            assert "run-shell" not in gated[key]
            assert "cursor_x" not in gated[key]

    def test_bare_arrows_gate_on_the_composer_via_the_probe(self):
        """cursor_x is a FAST PATH, never the decision. An empty composer
        parks the cursor at column 2, so >2 proves there is text and the
        key goes straight to Claude with no fork — but 2 proves nothing
        (start of a line, a multi-line composer, a wrapped row all sit
        there with text present), so that case defers to the probe. The
        earlier version treated 2 as "empty" and stole the key mid-line."""
        left, right = (" ".join(c) for c in
                       arrow_capture_commands(probe="/p/probe")[:2])
        for key, binding in (("Left", left), ("Right", right)):
            fast = f"if -F '#{{>:#{{cursor_x}},2}}' 'send-keys {key}'"
            assert fast in binding  # >2 => text => Claude's, no shell out
            assert "/p/probe" in binding and "run-shell" in binding
            assert "#{pane_id}" in binding and "#{window_zoomed_flag}" in binding
            # the probe reads the composer itself; it is handed no cursor
            assert "#{cursor_y}" not in binding
            # the size control is NOT reachable straight from cursor_x
            assert binding.index("run-shell") > binding.index(fast)

    def test_probe_script_is_the_module_under_a_shebang(self):
        """The probe is written out as a copy of composer_probe.py so the
        rule it applies is the one the tests exercise — same file, not a
        second implementation embedded in a string."""
        from cagents.sidecar import composer_probe_source

        source = composer_probe_source("/x/python3")
        assert source.startswith("#!/x/python3\n")
        assert "def composer_is_empty(" in source
        assert "from ." not in source  # a copy has no package to import from

    def test_no_probe_falls_back_to_the_unconditional_size_control(self):
        for cmd in arrow_capture_commands():
            assert "run-shell" not in " ".join(cmd)

    def test_modifier_arrows_walk_all_three_sizes_from_either_pane(self):
        """⌥ and ⇧ arrows never yield — the guaranteed path while the
        composer has text, or if the probe ever stops recognising Claude's
        screen. They cover the whole cycle including WIDE -> SMALL, which
        is a focus change rather than a resize and which the first cut
        missed. ⇧ is the one that costs nothing: measured against a real
        session, ⇧← is Claude's plain ← while ⌥←/⌃← are its word-wise
        movement."""
        for enabled in (True, False):
            keys = [f"{modifier}{arrow}"
                    for modifier in ("M-", "S-") for arrow in ("Left", "Right")]
            bound = {c[2]: " ".join(c) for c in arrow_capture_commands(bare=enabled)
                     if len(c) > 2 and c[2] in keys}
            assert set(bound) == set(keys), enabled
            for binding in bound.values():
                assert "cursor_x" not in binding and "run-shell" not in binding
            for modifier in ("M-", "S-"):
                left, right = bound[f"{modifier}Left"], bound[f"{modifier}Right"]
                assert "resize-pane -Z -t :.1 ; select-pane -t :.1" in left
                assert "select-pane -t :.0" in left
                assert "select-pane -t :.1" in right
                assert "resize-pane -Z -t :.1" in right

    def test_dim_chat_commands_enabled_sets_a_per_pane_style_only_on_the_chat_pane(self):
        from cagents.sidecar import dim_chat_commands

        commands = dim_chat_commands(True)
        flat = [" ".join(c) for c in commands]
        assert any("set-hook" in c and "after-select-pane" in c for c in flat)
        hook = next(c for c in flat if "set-hook" in c)
        # per-pane override (-p) on the chat pane (1), never the rail (0)
        assert "set-option -p -t :.1 window-style" in hook
        assert "set-option -p -t :.0" not in hook

    def test_dim_chat_commands_disabled_clears_any_leftover_style(self):
        from cagents.sidecar import dim_chat_commands

        commands = dim_chat_commands(False)
        flat = [" ".join(c) for c in commands]
        hook = next(c for c in flat if "set-hook" in c)
        assert "window-style" not in hook  # plain, undimmed focus hook
        assert ["set-option", "-p", "-t", ":.1", "-u", "window-style"] in commands

    def test_apply_dim_chat_is_best_effort(self):
        from cagents.sidecar import apply_dim_chat

        calls = []

        def runner(args):
            calls.append(args)
            raise RuntimeError("no pane yet")

        apply_dim_chat(True, runner=runner)  # must not raise
        assert calls  # still attempted

    def test_ctx_bind_commands(self):
        commands = ctx_bind_commands("/venv/bin/cagents-ctx", "/data/context.json")
        flat = [" ".join(c) for c in commands]
        assert any(c.startswith("bind -n C-t run-shell") and "shell" in c for c in flat)
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


async def test_enter_chat_binding_attaches_and_zooms_the_viewer(world):
    store, tmux, registry, claude_dir = world
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        app.action_enter_chat()
        await pilot.pause()
        # same as Enter: select the session tab + focus the pane...
        assert ["select-window", "-t", "=work:session"] in work.calls
        assert ["select-pane", "-t", "%1"] in outer.calls
        # ...but also zoomed to full width, unlike plain Enter.
        assert ["resize-pane", "-Z", "-t", "%1"] in outer.calls


async def test_enter_chat_skips_zoom_when_attach_fails(world):
    store, tmux, registry, claude_dir = world
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    sidecar = Sidecar(runner=outer, own_pane="%0", work_runner=work)
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=sidecar,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        # tmux unavailable -> _attach() notifies and returns None early;
        # no zoom should be attempted on a session that was never attached.
        resizes_before = sum(1 for c in outer.calls if c[0] == "resize-pane")
        monkeypatch_available = tmux.available
        tmux.available = lambda: False
        try:
            app.action_enter_chat()
            await pilot.pause()
        finally:
            tmux.available = monkeypatch_available
        assert sum(1 for c in outer.calls if c[0] == "resize-pane") == resizes_before


async def test_browsing_to_a_dead_session_resumes_the_real_cli(world, claude_dir, now):
    # There is no static/fake transcript rendering for a dead session —
    # settling on one while browsing resumes the real `claude --resume`
    # CLI right then (lazily: only this one, not every session in the
    # list) and shows THAT in the viewer.
    store, tmux, registry, _ = world
    sid_dead = "99999999-9999-9999-9999-999999999999"
    # cwd is a real, existing directory from the start — the transcript's
    # own recorded cwd (not tracked.project_dir) is what work_dir actually
    # resolves to, and mutating it on a live snapshot mid-test is racy
    # against the app's own background refresh.
    TranscriptBuilder(sid_dead, "/tmp").ai_title("Old work").user("x").assistant_text(
        "finished"
    ).write(claude_dir, mtime=now - 5000)
    store.track(sid_dead, "/tmp", "2026-08-18T07:00:00+00:00")
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, sid_dead)
        await pilot.pause(0.5)  # debounce
        assert tmux.created and tmux.created[-1][1][:2] == ["--resume", sid_dead]
        shown = [c[-1] for c in work.calls if c[0] == "respawn-pane"]
        assert any("attach-session" in cmd for cmd in shown)
        assert not any("--preview-session" in cmd for cmd in shown)

        # settling on it again (e.g. the debounce firing twice before the
        # next snapshot catches up) must not spawn a second resume
        spawned_before = len(tmux.created)
        select_session(app, sid_dead)
        await pilot.pause(0.5)
        assert len(tmux.created) == spawned_before


async def test_terminal_tab_belongs_to_the_selected_live_session(world, claude_dir, now, tmp_path):
    # Regression: "N" used to open one shell shared by the whole app,
    # never cd-ing into whichever session was actually selected. Each
    # live session must get its own persistent terminal window instead.
    store, tmux, registry, _ = world
    sid_beta = "77777777-7777-7777-7777-777777777777"
    real_dir = tmp_path / "beta-worktree"
    real_dir.mkdir()
    init_git_repo(real_dir)
    TranscriptBuilder(sid_beta, str(real_dir)).ai_title("Beta work").user(
        "go", ts=ts_ago(1)
    ).write(claude_dir, mtime=now - 1)
    store.track(sid_beta, str(real_dir), "2026-08-18T09:00:00+00:00")
    tmux.sessions.append(
        TmuxSession(name="beta", created=now - 60, activity=now, attached=False,
                    pane_pid=2, pane_path=str(real_dir), socket="claude")
    )
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, sid_beta)
        await pilot.pause(0.5)
        app.action_open_terminal()
        await pilot.pause()
        assert any(entry.startswith("ensure-window:beta:term:") for entry in tmux.log)
        assert "ensure-view:beta:term" in tmux.log
        respawn = [c for c in work.calls if c[0] == "respawn-pane" and "=work:term-1" in c][-1]
        assert respawn[-1] == "env -u TMUX tmux -L claude attach-session -t '=beta--term'"


async def test_terminal_tab_follows_browsing_without_pressing_n_again(
    world, claude_dir, now, tmp_path
):
    # The actual reported bug: with term-1 already open, just moving the
    # highlight through the list (never re-pressing "N", never clicking
    # the tab again) must still repoint term-1 at the newly selected
    # session — otherwise it keeps showing whatever session last
    # explicitly opened it, which looks exactly like one shared shell.
    store, tmux, registry, _ = world  # SID1 = "alpha", lives at "/proj/alpha" (not real)
    alpha_dir = tmp_path / "alpha-worktree"
    alpha_dir.mkdir()
    init_git_repo(alpha_dir)
    TranscriptBuilder(SID1, str(alpha_dir)).ai_title("Alpha: fix auth").user(
        "go", ts=ts_ago(1)
    ).write(claude_dir, mtime=now - 1)  # overwrite with a real, existing worktree
    store.sessions[SID1].project_dir = str(alpha_dir)
    tmux.sessions[0].pane_path = str(alpha_dir)  # the "alpha" tmux session, re-pointed

    sid_beta = "66666666-6666-6666-6666-666666666666"
    real_dir = tmp_path / "beta-worktree"
    real_dir.mkdir()
    init_git_repo(real_dir)
    TranscriptBuilder(sid_beta, str(real_dir)).ai_title("Beta work").user(
        "go", ts=ts_ago(1)
    ).write(claude_dir, mtime=now - 1)
    store.track(sid_beta, str(real_dir), "2026-08-18T09:00:00+00:00")
    tmux.sessions.append(
        TmuxSession(name="beta", created=now - 60, activity=now, attached=False,
                    pane_pid=2, pane_path=str(real_dir), socket="claude")
    )
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, sid_beta)
        await pilot.pause(0.5)
        app.action_open_terminal()  # explicitly open it once, on beta
        await pilot.pause()
        respawn = [c for c in work.calls if c[0] == "respawn-pane" and "=work:term-1" in c][-1]
        assert "beta--term" in respawn[-1]

        # now just browse away — no "N", no tab click
        select_session(app, SID1)
        await pilot.pause(0.5)  # the same viewer-sync debounce, not a new action
        respawn = [c for c in work.calls if c[0] == "respawn-pane" and "=work:term-1" in c][-1]
        assert "alpha--term" in respawn[-1]


async def test_terminal_errors_instead_of_falling_back_when_worktree_is_missing(world):
    # A live session whose recorded worktree no longer exists on disk (a
    # session tracked from a path that's since moved/vanished) must not
    # silently open a generic shell somewhere else — it's an explicit
    # error, and no per-session terminal setup is attempted.
    from textual.widgets._toast import Toast

    store, tmux, registry, claude_dir = world  # SID1 lives at "/proj/alpha" (not real)
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await pilot.pause(0.5)
        select_session(app, SID1)
        await pilot.pause(0.5)
        work.calls.clear()
        app.action_open_terminal()
        await pilot.pause(0.2)
        assert not any(entry.startswith("ensure-window:") for entry in tmux.log)
        assert not any(c[0] == "respawn-pane" and "=work:term-1" in c for c in work.calls)
        # quiet by design (user choice): no toast for a missing worktree
        assert not list(app.query(Toast))


async def test_terminal_errors_when_no_session_is_selected(world):
    from textual.widgets._toast import Toast

    store, tmux, registry, claude_dir = world
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await pilot.pause(0.5)
        app.selected_session_id = "not-a-real-session-id"
        work.calls.clear()
        app.action_open_terminal()
        await pilot.pause(0.2)
        assert not any(c[0] == "respawn-pane" and "=work:term-1" in c for c in work.calls)
        toasts = list(app.query(Toast))
        assert any("no session selected" in t.render().plain.lower() for t in toasts)


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
    hook = next(
        c for c in work.calls
        if c[0] == "set-hook" and "after-select-window" in " ".join(c) and "if -F" in " ".join(c)
    )
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
    are never touched either way.

    The tabbed workspace (WORK_SOCKET) is the one thing quit must NOT
    tear down: terminal tabs (and whatever's running in them) are meant
    to persist across a cagents restart."""

    def _app(self, world) -> CagentsApp:
        store, tmux, registry, claude_dir = world
        return CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)

    def test_teardown_kills_only_the_container_socket(self, world):
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

        assert ["tmux", "-L", CONTAINER_SOCKET, "kill-server"] in calls
        # WORK_SOCKET deliberately survives — terminal tabs persist across
        # a cagents restart, so quitting must never kill that server.
        assert ["tmux", "-L", WORK_SOCKET, "kill-server"] not in calls
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


def test_workspace_disables_wheel_tab_switching():
    """tmux's default WheelUp/DownStatus binds switch windows when the wheel
    drifts over the tab bar — the top of the claude pane, exactly where you
    scroll. That read as phantom tab switches; the workspace must unbind both."""
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    sidecar = Sidecar(runner=outer, own_pane="%0", work_runner=work)
    sidecar.ensure_workspace("/tmp", "/bin/cagents-ctx", "/data/context.json")
    flat = [" ".join(map(str, c)) for c in work.calls]
    assert any(c == "unbind -n WheelUpStatus" for c in flat)
    assert any(c == "unbind -n WheelDownStatus" for c in flat)
    # forensics: every window change / select / status-line mouse event is
    # logged through cagents-ctx wlog (quoting-proof), and tab clicks keep
    # their default switch after logging.
    assert any("session-window-changed" in c and "wlog window-changed:" in c for c in flat)
    assert any("wlog select-window:" in c for c in flat)
    assert any("WheelUpStatus run-shell -b" in c and "wlog wheel-up-status" in c for c in flat)
    assert any(
        "MouseDown1Status" in c and "wlog status-click:" in c and "switch-client -t =" in c
        for c in flat
    )


def test_window_view_selects_only_on_create_or_explicit_open():
    """The passive per-refresh terminal sync must not re-select the grouped
    session's window every 2s (log spam + snaps the view back)."""
    from cagents.tmuxctl import TmuxClient

    calls = []

    class Probe(TmuxClient):
        def __init__(self):
            super().__init__()
            self._sessions = set()

        def _run(self, socket, *args, timeout=5.0):
            calls.append(args)
            import subprocess

            if args[0] == "has-session":
                ok = args[2].lstrip("=") in self._sessions
                return subprocess.CompletedProcess(args, 0 if ok else 1, "", "")
            if args[0] == "new-session":
                self._sessions.add(args[3])
            return subprocess.CompletedProcess(args, 0, "", "")

    client = Probe()
    client.ensure_window_view("alpha", "term")  # creates -> selects once
    selects = [c for c in calls if c[0] == "select-window"]
    assert len(selects) == 1
    client.ensure_window_view("alpha", "term")  # passive resync -> no select
    assert len([c for c in calls if c[0] == "select-window"]) == 1
    client.ensure_window_view("alpha", "term", force_select=True)  # explicit open
    assert len([c for c in calls if c[0] == "select-window"]) == 2


async def test_a_stale_viewer_sync_never_yanks_the_pane_back(world, claude_dir, now):
    """Seen live: one click made the session pane flip new -> old -> new,
    three respawn-panes and a full redraw each. The passive per-refresh sync
    (every 2s, re-emitted on each list rebuild) runs in a thread that
    captures the view it was scheduled for; when a click lands while one is
    mid-tmux-round-trip, the click points the pane at the new session, then
    the stale thread finishes, sees its (old) target differs from the pane,
    and "corrects" it back — after which the newer sync corrects it again.
    Slower tmux (a swapping machine) widens the window, so it got worse
    exactly when everything else did. A sync must apply only if its view is
    still the selected one."""
    store, tmux, registry, claude_dir = world
    TranscriptBuilder(SID2, "/proj/beta").ai_title("Beta").user("go", ts=ts_ago(1)).write(
        claude_dir, mtime=now - 1
    )
    store.track(SID2, "/proj/beta", "2026-08-18T09:05:00+00:00")
    tmux.sessions.append(
        TmuxSession(name="beta", created=now - 60, activity=now, attached=False,
                    pane_pid=2, pane_path="/proj/beta", socket="claude")
    )
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        alpha = app.snapshot.by_id(SID1)
        beta = app.snapshot.by_id(SID2)
        assert alpha is not None and beta is not None
        # The click already pointed the pane at beta and selected it...
        app.selected_session_id = SID2
        app.sidecar.show_viewer(app._viewer_command(beta))
        app._viewer_target = app._viewer_command(beta)
        before = [c[-1] for c in work.calls if c[0] == "respawn-pane" and "=work:session" in c]
        # ...and now the sync that was scheduled for alpha finally runs.
        app.run_worker(
            lambda: app._sync_viewer_blocking(alpha),
            thread=True, group="viewer-sync", exclusive=True, exit_on_error=False,
        )
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        after = [c[-1] for c in work.calls if c[0] == "respawn-pane" and "=work:session" in c]
        assert after == before, f"stale sync re-pointed the pane: {after[len(before):]}"
        assert app._viewer_target == app._viewer_command(beta)


async def test_a_stale_viewer_sync_never_yanks_the_terminal_tab_back(
    world, claude_dir, now, tmp_path
):
    """The session pane was only half of the flip. _sync_terminal does three
    tmux writes with round-trips between them, and it runs BEFORE the
    viewer's own staleness check — so a click landing mid-round-trip could
    still let a superseded sync re-point term-1 at the old session. That
    respawn is right there in the log the fix was written from:

        respawn-pane -k -t =work:term-1 … attach-session -t '=assistant-6--term'

    The entry check can't catch this one: the sync is perfectly fresh when
    it starts, and only goes stale partway through. Here the first tmux
    write stands in for "a click landed", the way it does live."""
    store, tmux, registry, _ = world  # SID1 = "alpha", at "/proj/alpha" (not real)
    alpha_dir = tmp_path / "alpha-worktree"
    alpha_dir.mkdir()
    init_git_repo(alpha_dir)
    TranscriptBuilder(SID1, str(alpha_dir)).ai_title("Alpha: fix auth").user(
        "go", ts=ts_ago(1)
    ).write(claude_dir, mtime=now - 1)
    store.sessions[SID1].project_dir = str(alpha_dir)
    tmux.sessions[0].pane_path = str(alpha_dir)

    beta_dir = tmp_path / "beta-worktree"
    beta_dir.mkdir()
    init_git_repo(beta_dir)
    TranscriptBuilder(SID2, str(beta_dir)).ai_title("Beta").user("go", ts=ts_ago(1)).write(
        claude_dir, mtime=now - 1
    )
    store.track(SID2, str(beta_dir), "2026-08-18T09:05:00+00:00")
    tmux.sessions.append(
        TmuxSession(name="beta", created=now - 60, activity=now, attached=False,
                    pane_pid=2, pane_path=str(beta_dir), socket="claude")
    )
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, SID1)
        await pilot.pause(0.5)
        app.action_open_terminal()  # term-1 open, pointed at alpha
        await pilot.pause(0.3)
        alpha = app.snapshot.by_id(SID1)
        assert alpha is not None

        # The click: it lands while the sync is inside _sync_terminal,
        # between its round-trips — after which nothing this sync does to
        # term-1 is wanted any more.
        real_ensure = tmux.ensure_session_window

        def ensure_then_click(*args, **kwargs):
            real_ensure(*args, **kwargs)
            app.selected_session_id = SID2

        tmux.ensure_session_window = ensure_then_click
        tmux.log.clear()
        before = [c[-1] for c in work.calls if c[0] == "respawn-pane" and "=work:term-1" in c]
        app.run_worker(
            lambda: app._sync_viewer_blocking(alpha),
            thread=True, group="viewer-sync", exclusive=True, exit_on_error=False,
        )
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert "ensure-view:alpha:term" not in tmux.log
        after = [c[-1] for c in work.calls if c[0] == "respawn-pane" and "=work:term-1" in c]
        assert after == before, f"stale sync re-pointed term-1: {after[len(before):]}"


async def test_browsing_stays_hands_off_when_resume_on_browse_is_off(world, claude_dir, now):
    """The lazy resume is a real process per row you settle on (~200MB+ of
    claude each). With resume_on_browse off, browsing must not start
    anything — a stopped session stays stopped until an explicit Enter."""
    store, tmux, registry, _ = world
    store.set_setting("resume_on_browse", False)
    sid_dead = "99999999-9999-9999-9999-999999999999"
    TranscriptBuilder(sid_dead, "/tmp").ai_title("Old work").user("x").assistant_text(
        "finished"
    ).write(claude_dir, mtime=now - 5000)
    store.track(sid_dead, "/tmp", "2026-08-18T07:00:00+00:00")
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        sidecar=Sidecar(runner=outer, own_pane="%0", work_runner=work),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, sid_dead)
        await pilot.pause(0.5)  # the same debounce that would have resumed it
        assert tmux.created == []
        # Turning the setting on mid-session works without a restart: the
        # one-shot "tried once" guard must not have been consumed.
        store.set_setting("resume_on_browse", True)
        select_session(app, SID1)
        await pilot.pause(0.3)
        select_session(app, sid_dead)
        await pilot.pause(0.5)
        assert tmux.created and tmux.created[-1][1][:2] == ["--resume", sid_dead]
