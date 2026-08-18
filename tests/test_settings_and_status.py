"""Tests for the settings panel, notification gating, left-arrow capture,
and the todo view's did/needs lines + done section."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SID1, SID2, TranscriptBuilder
from test_app import FakeTmux, render_text

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry, SessionState, derive_did_needs
from cagents.sidecar import left_capture_commands
from cagents.store import SETTINGS_DEFAULTS, Store
from cagents.tmuxctl import TmuxSession, extract_prompt_question
from cagents.views import SessionList


# ------------------------------------------------------------- settings ---


class TestSettingsStore:
    def test_defaults(self, tmp_path: Path):
        store = Store.load(tmp_path / "state.json")
        assert store.get_setting("sidebar") is True
        assert store.get_setting("notifications") is False  # default off
        assert store.get_setting("capture_left") is True

    def test_roundtrip_and_unknown_keys(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = Store.load(path)
        store.set_setting("notifications", True)
        store.set_setting("sidebar", False)
        store.set_setting("bogus", True)  # silently ignored
        reloaded = Store.load(path)
        assert reloaded.get_setting("notifications") is True
        assert reloaded.get_setting("sidebar") is False
        assert "bogus" not in reloaded.settings

    def test_defaults_cover_all_meta(self):
        from cagents.modals import SETTINGS_META

        assert {k for k, _, _ in SETTINGS_META} == set(SETTINGS_DEFAULTS)


def test_left_capture_commands():
    on = left_capture_commands(True)
    assert on[0][:3] == ["bind", "-n", "Left"]
    joined = " ".join(on[0])
    # In the session pane Left kills the pane; in the rail it passes through.
    assert "kill-pane" in joined and "send-keys Left" in joined
    off = left_capture_commands(False)
    assert off == [["unbind", "-n", "Left"]]


# ------------------------------------------------------------ did/needs ---


def test_extract_prompt_question():
    pane = "│ Bash command: rm -rf build │\n│ Do you want to proceed? │\n│ ❯ 1. Yes │"
    got = extract_prompt_question(pane)
    assert "Do you want to proceed?" in got
    assert "❯" not in got  # answer rows are skipped
    assert extract_prompt_question("just working…") == ""


class TestDeriveDidNeeds:
    def _parsed(self, claude_dir, text="All tests pass now."):
        b = TranscriptBuilder(SID1, "/proj/a").user("go").assistant_text(text)
        from cagents.claude_data import parse_session_file

        return parse_session_file(b.write(claude_dir))

    def test_working_has_no_lines(self, claude_dir):
        parsed = self._parsed(claude_dir)
        assert derive_did_needs(SessionState.WORKING, "thinking", parsed) == ("", "")
        assert derive_did_needs(SessionState.DONE, "", parsed) == ("", "")
        assert derive_did_needs(SessionState.STOPPED, "", parsed) == ("", "")

    def test_needs_review(self, claude_dir):
        parsed = self._parsed(claude_dir, "Refactor complete.\nDetails below…")
        did, needs = derive_did_needs(SessionState.NEEDS_REVIEW, "", parsed)
        assert did == "Refactor complete."  # single line only
        assert "review" in needs

    def test_needs_input_prefers_pane_question(self, claude_dir):
        parsed = self._parsed(claude_dir)
        did, needs = derive_did_needs(
            SessionState.NEEDS_INPUT,
            "permission: Bash",
            parsed,
            pane_text="Do you want to run make deploy?\n❯ 1. Yes",
        )
        assert needs == "Do you want to run make deploy?"
        did2, needs2 = derive_did_needs(SessionState.NEEDS_INPUT, "permission: Bash", parsed)
        assert needs2 == "permission: Bash"  # fallback without a pane


# ------------------------------------------------------------- UI world ---


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    # SID1: finished -> needs review. SID2: mid-turn, live -> working.
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Fix auth").user("go").assistant_text(
        "Fixed the token check; added a regression test."
    ).write(claude_dir, mtime=now - 900)
    TranscriptBuilder(SID2, "/proj/beta").ai_title("Refactor db").user("go").assistant_tool_use(
        "t1", "Bash", {"command": "pytest"}
    ).write(claude_dir, mtime=now - 1)

    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.track(SID2, "/proj/beta", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(name="beta", created=now - 60, activity=now, attached=False,
                    pane_pid=1, pane_path="/proj/beta")
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


def _todo_rows(app) -> str:
    todos_list = app.query_one("#todos-list", SessionList)
    return "\n".join(
        render_text(todos_list.get_option_at_index(i).prompt)
        for i in range(todos_list.option_count)
    )


async def test_todo_did_needs_lines_for_review(world):
    app, store, _ = world
    todo = store.add_todo("auth work", "2026-08-17T09:00:00+00:00", "/proj/alpha")
    store.link_todo_session(todo.todo_id, SID1)  # needs review
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        rows = _todo_rows(app)
        assert "did" in rows
        assert "Fixed the token check; added a regression test." in rows
        assert "needs" in rows
        assert "your review" in rows


async def test_todo_no_lines_while_working(world):
    app, store, _ = world
    todo = store.add_todo("db work", "2026-08-17T09:00:00+00:00", "/proj/beta")
    store.link_todo_session(todo.todo_id, SID2)  # working
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        rows = _todo_rows(app)
        # the agent is mid-turn: no did/needs sub-rows at all
        assert "did    " not in rows
        assert "needs  " not in rows
        assert "db work" in rows  # the todo itself renders


async def test_done_section_divider_and_grey(world):
    app, store, _ = world
    store.add_todo("open one", "2026-08-17T09:00:00+00:00")
    done = store.add_todo("finished one", "2026-08-17T08:00:00+00:00")
    store.set_todo_done(done.todo_id, "2026-08-17T10:00:00+00:00")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        rows = _todo_rows(app)
        assert "── done" in rows
        # done todo renders below the divider
        assert rows.index("open one") < rows.index("── done") < rows.index("finished one")


async def test_notifications_gated_by_setting(world):
    app, store, _ = world
    captured = []
    import textual.app as textual_app

    original = textual_app.App.notify

    def spy(self, message, **kwargs):
        captured.append((kwargs.get("severity", "information"), str(message)))

    textual_app.App.notify = spy
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            captured.clear()
            app.notify("routine info")  # default: notifications off
            app.notify("careful", severity="warning")
            app.notify("boom", severity="error")
            assert [s for s, _ in captured] == ["warning", "error"]

            store.set_setting("notifications", True)
            app.notify("routine info")
            assert captured[-1] == ("information", "routine info")
    finally:
        textual_app.App.notify = original


async def test_settings_modal_toggles_and_persists(world, tmp_path):
    app, store, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        from cagents.modals import SettingsModal

        assert isinstance(app.screen, SettingsModal)
        # first row is "sidebar" (default on) -> toggle off
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert store.get_setting("sidebar") is False
        assert Store.load(store.path).get_setting("sidebar") is False  # persisted
        await pilot.press("enter")  # toggle back on
        await pilot.pause(0.1)
        assert store.get_setting("sidebar") is True
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsModal)


async def test_sidebar_setting_off_uses_suspend_attach(world, now):
    from test_sidecar import FakeOuterTmux
    from cagents.sidecar import Sidecar

    app, store, tmux = world
    outer = FakeOuterTmux()
    app.sidecar = Sidecar(runner=outer, own_pane="%0")
    store.set_setting("sidebar", False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from test_app import select_session

        select_session(app, SID2)  # the live one
        await pilot.pause()
        app.action_attach()
        await pilot.pause()
        # sidebar off -> classic suspend attach, no pane split
        assert tmux.attached_to == ["beta"]
        assert not any(c[0] == "split-window" for c in outer.calls)


# ------------------------------------------------- transient "needs you" --


class TestPromptFalsePositives:
    def test_conversation_text_is_not_a_prompt(self):
        from cagents.tmuxctl import pane_shows_prompt

        # Claude *talking about* choices must not read as a dialog
        assert pane_shows_prompt("Do you want me to also refactor the parser?") is False
        assert pane_shows_prompt("Would you like a summary when I finish?") is False
        # ...and a numbered list in output isn't one either
        assert pane_shows_prompt("Steps:\n 1. build\n 2. deploy") is False

    def test_real_dialog_is_a_prompt(self):
        from cagents.tmuxctl import pane_shows_prompt

        pane = "Bash command: make deploy\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
        assert pane_shows_prompt(pane) is True

    def test_choice_row_without_phrase_is_not_enough(self):
        from cagents.tmuxctl import pane_shows_prompt

        assert pane_shows_prompt("❯ 1. some quoted menu") is False


class TestDebounce:
    def _registry(self, claude_dir, tmp_path, now, pane):
        from test_app import FakeTmux

        TranscriptBuilder(SID1, "/proj/alpha").user("go").assistant_tool_use(
            "t1", "Bash", {"command": "ls"}
        ).write(claude_dir, mtime=now - 1)
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
        tmux = FakeTmux()
        tmux.sessions.append(
            TmuxSession(name="alpha", created=now - 60, activity=now, attached=False,
                        pane_pid=1, pane_path="/proj/alpha")
        )
        tmux.panes["alpha"] = pane
        return SessionRegistry(store, tmux=tmux, claude_dir=claude_dir), tmux

    def test_working_to_input_held_one_cycle(self, claude_dir, tmp_path, now):
        registry, tmux = self._registry(claude_dir, tmp_path, now, "✻ Running… (esc to interrupt)")
        assert registry.refresh(now=now).views[0].state == SessionState.WORKING
        # pane flips to a dialog
        tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
        held = registry.refresh(now=now + 2).views[0]
        assert held.state == SessionState.WORKING  # held one cycle
        confirmed = registry.refresh(now=now + 4).views[0]
        assert confirmed.state == SessionState.NEEDS_INPUT  # confirmed
        assert confirmed.needs_line == "Do you want to proceed?"

    def test_flicker_never_surfaces(self, claude_dir, tmp_path, now):
        registry, tmux = self._registry(claude_dir, tmp_path, now, "✻ Running… (esc to interrupt)")
        registry.refresh(now=now)
        tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
        registry.refresh(now=now + 2)  # held
        tmux.panes["alpha"] = "✻ Running… (esc to interrupt)"  # dialog resolved itself
        back = registry.refresh(now=now + 4).views[0]
        assert back.state == SessionState.WORKING
        # streak must have reset: a later real dialog is held exactly once again
        tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
        assert registry.refresh(now=now + 6).views[0].state == SessionState.WORKING
        assert registry.refresh(now=now + 8).views[0].state == SessionState.NEEDS_INPUT

    def test_first_observation_trusted_immediately(self, claude_dir, tmp_path, now):
        registry, _ = self._registry(claude_dir, tmp_path, now, "Do you want to proceed?\n❯ 1. Yes")
        # fresh startup with a visible dialog: no artificial delay
        assert registry.refresh(now=now).views[0].state == SessionState.NEEDS_INPUT


# ------------------------------------ left capture in fullscreen mode -----


def test_left_detach_bind_args_are_tty_filtered():
    from cagents.tmuxctl import left_detach_bind_args

    args = left_detach_bind_args("/dev/ttys009")
    assert args[:3] == ["bind", "-n", "Left"]
    joined = " ".join(args)
    assert "#{==:#{client_tty},/dev/ttys009}" in joined
    assert "detach-client" in joined
    assert "send-keys Left" in joined  # other clients pass through


class RecordingTmux(FakeTmux):
    def __init__(self):
        super().__init__()
        self.log: list[str] = []

    def attach(self, name: str) -> int:
        self.log.append(f"attach:{name}")
        return super().attach(name)

    def bind_left_detach(self, client_tty: str) -> None:
        self.log.append(f"bind:{client_tty}")

    def unbind_left_detach(self) -> None:
        self.log.append("unbind")


async def test_fullscreen_attach_binds_left_when_enabled(claude_dir, tmp_path, now):
    TranscriptBuilder(SID1, "/proj/alpha").user("go").write(claude_dir, mtime=now - 1)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    store.set_setting("sidebar", False)  # the reported scenario
    tmux = RecordingTmux()
    tmux.sessions.append(
        TmuxSession(name="alpha", created=now - 60, activity=now, attached=False,
                    pane_pid=1, pane_path="/proj/alpha")
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    app._current_tty = lambda: "/dev/ttys009"  # tests have no real tty

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_attach()
        await pilot.pause()
        # bind wraps the attach, unbind always follows
        assert tmux.log == ["bind:/dev/ttys009", "attach:alpha", "unbind"]

        # capture off -> plain attach, no binding
        tmux.log.clear()
        store.set_setting("capture_left", False)
        app.action_attach()
        await pilot.pause()
        assert tmux.log == ["attach:alpha"]
