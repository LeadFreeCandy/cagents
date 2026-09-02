"""End-to-end UI tests driven through Textual's pilot.

A fake tmux client and a temp Claude store give the app a fully
controlled world; no real tmux server or ~/.claude is touched. These run
with sidecar=None (fullscreen mode) unless a test injects a Sidecar —
sidecar-specific behavior lives in test_sidecar.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    SID1,
    SID2,
    SID3,
    FakeTmux,
    TranscriptBuilder,
    render_text,
    select_session,
    ts_ago,
    widget_text,
)

from cagents.app import CagentsApp
from cagents.sessions import SessionRegistry, SessionState
from cagents.store import Store
from cagents.tmuxctl import TmuxSession
from cagents.views import KanbanView, SessionList


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    """Three sessions in two projects, one live in tmux."""
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user("go").assistant_text(
        "done with auth"
    ).write(claude_dir, mtime=now - 900)
    TranscriptBuilder(SID2, "/proj/alpha").ai_title("Alpha: add tests").user("go").assistant_tool_use(
        "t1", "Bash", {"command": "pytest"}, ts=ts_ago(2)
    ).write(claude_dir, mtime=now - 2)
    TranscriptBuilder(SID3, "/proj/beta").ai_title("Beta: refactor").user("go").assistant_text(
        "refactored"
    ).write(claude_dir, mtime=now - 4000)

    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    store.track(SID2, "/proj/alpha", "2026-08-18T09:10:00+00:00")
    store.track(SID3, "/proj/beta", "2026-08-18T08:00:00+00:00")

    tmux = FakeTmux()
    tmux.sessions.append(
        TmuxSession(
            name="alpha", created=now - 300, activity=now, attached=False,
            pane_pid=42, pane_path="/proj/alpha", socket="claude",
        )
    )
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux, claude_dir


async def test_startup_shows_queue_default(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # View 1 = queue is the default; no persisted view state exists.
        assert app.active_view_id == "queue"
        assert app.snapshot.views and len(app.snapshot.views) == 3
        by_id = {v.session_id: v for v in app.snapshot.views}
        assert by_id[SID2].state == SessionState.WORKING
        assert by_id[SID2].live is True
        assert by_id[SID2].tmux_socket == "claude"  # discovered on the wrapper socket
        assert by_id[SID1].state == SessionState.NEEDS_REVIEW
        assert by_id[SID3].state == SessionState.NEEDS_REVIEW

        grouped = app.query_one("#grouped-list", SessionList)
        assert grouped.option_count == 5  # 2 group headers + 3 rows
        summary = widget_text(app, "#summary")
        assert "3 sessions" in summary
        assert "1 working" in summary
        assert "2 review" in summary


async def test_navigation_updates_preview(world):
    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = app.selected_session_id
        await pilot.press("j")
        await pilot.pause()
        assert app.selected_session_id != first
        assert widget_text(app, "#preview-content")  # fullscreen mode renders internally


async def test_view_switching_cycles_three_views(world):
    app, *_ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.active_view_id == "queue"  # 1 = queue, the default
        await pilot.press("2")
        await pilot.pause()
        assert app.active_view_id == "grouped"
        await pilot.press("3")
        await pilot.pause()
        assert app.active_view_id == "kanban"
        await pilot.press("tab")
        await pilot.pause()
        assert app.active_view_id == "queue"
        await pilot.press("1")
        await pilot.pause()
        assert app.active_view_id == "queue"


async def test_queue_orders_by_attention(world):
    app, store, tmux, claude_dir = world
    tmux.panes["alpha"] = "Do you want to proceed?\n❯ 1. Yes"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        queue = app.query_one("#queue-list", SessionList)
        assert queue.get_option_at_index(0).id == SID2  # needs input first
        assert app.snapshot.by_id(SID2).state == SessionState.NEEDS_INPUT


async def test_kanban_columns_and_arrow_navigation(world):
    app, *_ = world
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        working = app.query_one("#kb-working", SessionList)
        review = app.query_one("#kb-review", SessionList)
        assert working.option_count == 1
        assert review.option_count == 2
        kanban = app.query_one("#kanban", KanbanView)
        start = kanban.active_column
        await pilot.press("right")
        await pilot.pause()
        assert kanban.active_column != start  # arrows move between columns


async def test_attach_live_session_fullscreen(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)
        await pilot.pause()
        app.action_attach()
        await pilot.pause()
        # Attached on the session's own socket, statusline shown+restored.
        assert tmux.attached_to == [("alpha", "claude")]
        assert "status-on:alpha" in tmux.log and "status-off:alpha" in tmux.log


async def test_attach_dead_session_resumes_on_private_socket(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID3)
        await pilot.pause()
        # /proj/beta doesn't exist on disk -> loud failure, no attach
        app.action_attach()
        await pilot.pause()
        assert tmux.attached_to == []

        store.sessions[SID3].project_dir = "/tmp"
        view = app.snapshot.by_id(SID3)
        if view.parsed:
            view.parsed.cwd = "/tmp"
        app.action_attach()
        await pilot.pause()
        assert tmux.created and tmux.created[-1][1][:2] == ["--resume", SID3]
        # every spawn carries the state hooks (the anti-flap authority)
        assert "--settings" in tmux.created[-1][1]
        settings_json = tmux.created[-1][1][tmux.created[-1][1].index("--settings") + 1]
        assert "Notification" in settings_json and "Stop" in settings_json
        # Resume spawns on the PRIVATE socket (spawning next to a live
        # claude on a shared socket crashes it).
        assert tmux.attached_to[-1][1] == "cagents-sessions"


async def test_done_key_flow(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # needs review
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at != ""
        assert app.snapshot.by_id(SID1).state == SessionState.DONE
        # The cursor must NOT have followed SID1 down to its new,
        # sunk-to-the-bottom spot — it stays at the same row index, which
        # now belongs to whatever moved up into SID1's old place.
        assert app.selected_session_id != SID1
        await pilot.press("d")  # un-done, now acting on the row under the cursor
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at != ""  # untouched by the second press


async def test_done_un_done_on_the_same_row(world):
    """Re-selecting the just-done session and pressing d again does un-done
    it, and its cursor does not follow it away from its new, un-done spot."""
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # needs review
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(0.1)
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("d")  # un-done
        await pilot.pause(0.1)
        assert store.sessions[SID1].reviewed_at == ""
        assert app.snapshot.by_id(SID1).state == SessionState.NEEDS_REVIEW


async def test_done_refuses_while_working(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)  # working
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(0.1)
        assert store.sessions[SID2].reviewed_at == ""


async def test_rename(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press(*"auth work")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert store.sessions[SID1].label == "auth work"
        assert app.snapshot.by_id(SID1) is not None


async def test_untrack_with_confirm(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID3)
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("n")  # decline
        await pilot.pause(0.1)
        assert SID3 in store.sessions
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause(0.1)
        assert SID3 not in store.sessions


async def test_app_keys_do_not_fire_inside_modal(world):
    app, store, tmux, _ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("q")  # typed into the rename input, must not quit
        await pilot.pause()
        assert app.is_running
        await pilot.press("escape")
        await pilot.pause()
        assert store.sessions[SID1].label == ""


async def test_new_session_opens_a_shell_terminal_in_launch_cwd(world, tmp_path, monkeypatch):
    # No modal at all now: `n` opens a plain shell (not claude) already
    # tracked under a fresh session id, right in CAGENTS_LAUNCH_CWD.
    app, store, tmux, _ = world
    launch_dir = tmp_path / "launchhere"
    launch_dir.mkdir()
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(launch_dir))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # selection must NOT influence the default
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.2)
        directory, sid = tmux.shell_created[-1]
        assert directory == str(launch_dir)
        assert sid in store.sessions
        assert store.sessions[sid].project_dir == str(launch_dir)
        # a claude session, not a real claude process, was started
        assert tmux.created == [] or tmux.created[-1][2] != sid


async def test_new_session_seeds_the_terminal_with_recent_directory_shortcuts(
    world, tmp_path, monkeypatch
):
    app, store, tmux, _ = world
    older = tmp_path / "older-proj"
    newer = tmp_path / "newer-proj"
    older.mkdir()
    newer.mkdir()
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(tmp_path / "fresh"))
    (tmp_path / "fresh").mkdir()
    store.track("11111111-0000-0000-0000-000000000001", str(older), "2026-08-17T09:00:00+00:00")
    store.track("11111111-0000-0000-0000-000000000002", str(newer), "2026-08-17T10:00:00+00:00")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.2)
        name, command = tmux.shell_commands[-1]
        assert name == tmux.sessions[-1].name
        # newest tracked directory listed first, each with a numbered
        # cd-shortcut alias
        assert command.index(str(newer)) < command.index(str(older))
        assert "alias 1=" in command
        assert "alias 2=" in command


async def test_track_modal_lists_untracked(world, claude_dir, now):
    app, store, tmux, _ = world
    sid_new = "44444444-4444-4444-4444-444444444444"
    TranscriptBuilder(sid_new, "/proj/gamma").ai_title("Gamma work").user("hi").write(
        claude_dir, mtime=now - 50
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause(0.2)
        from cagents.modals import TrackModal

        assert isinstance(app.screen, TrackModal)
        await pilot.press("enter")  # filter -> focuses list
        await pilot.pause()
        await pilot.press("enter")  # select the only candidate
        await pilot.pause(0.1)
        assert sid_new in store.sessions
        assert store.sessions[sid_new].project_dir == "/proj/gamma"


async def test_session_rows_never_fold_onto_a_second_line(world):
    """format.session_row asks for no_wrap/ellipsis, but Textual reads those
    from CSS for Content prompts and ignores the Rich Text's own flags — so
    without the rule on the widget every wide row folds in half."""
    from textual.visual import visualize

    from cagents.views import SessionList

    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for list_id in ("#queue-list", "#grouped-list"):
            session_list = app.query_one(list_id, SessionList)
            rules = session_list.styles.get_render_rules()
            assert session_list.option_count
            for index in range(session_list.option_count):
                prompt = session_list.get_option_at_index(index).prompt
                # Far narrower than a row needs: it must ellipsize, not fold.
                height = visualize(session_list, prompt).get_height(rules, 40)
                assert height == 1, f"{list_id} row {index} folded to {height} lines"


async def test_list_columns_size_to_the_visible_titles(world):
    """The world's titles are all short; the state column must sit right
    after the widest of them, not 44 columns out — and on the same column
    in every row."""
    import re

    from cagents.format import TITLE_MIN
    from cagents.views import SessionList

    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        longest = max(len(view.title) for view in app.snapshot.views)
        for list_id in ("#queue-list", "#grouped-list"):
            session_list = app.query_one(list_id, SessionList)
            rows = [
                render_text(session_list.get_option_at_index(i).prompt)
                for i in range(session_list.option_count)
                if session_list.get_option_at_index(i).id  # skip group headers
            ]
            assert rows
            for row in rows:
                assert "            " not in row, f"{list_id}: {row!r}"  # 12 blanks = the old pad
            state_cols = {
                re.search(r"needs you|working|review|done|stopped|background", row).start()
                for row in rows
            }
            assert state_cols == {3 + max(longest, TITLE_MIN) + 2}


async def test_kanban_cards_still_wrap(world):
    """kanban_card is deliberately multi-line in a narrow column — it must keep
    folding rather than inherit the flat lists' ellipsis, which would cut every
    title off at the column edge."""
    from textual.visual import visualize

    from cagents.views import KanbanView, SessionList

    app, store, *_ = world
    store.set_label(SID1, "a very long session name that cannot fit in one narrow column")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.refresh_data()
        await pilot.pause(0.2)
        heights = []
        for session_list in app.query_one(KanbanView).query(SessionList):
            rules = session_list.styles.get_render_rules()
            for index in range(session_list.option_count):
                prompt = session_list.get_option_at_index(index).prompt
                heights.append(visualize(session_list, prompt).get_height(rules, 20))
        # kanban_card writes 3 explicit lines (title / project · age / detail),
        # so nowrap caps every card at 3 no matter how long the title is. The
        # long name above must push past that by folding.
        assert heights and max(heights) > 3, heights
async def test_d_marks_a_needs_you_session_done(world, claude_dir, now):
    """The guard used to refuse with "Still in flight" — an imported session
    parked on Claude's resume dialog could not be dismissed at all."""
    app, store, tmux, _ = world
    # Two sessions share /proj/alpha, so the tmux mapping content-verifies:
    # the pane must show text from SID2's own transcript to claim it.
    tmux.panes["alpha"] = "Alpha: add tests\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        select_session(app, SID2)  # the live one (tmux "alpha")
        await pilot.pause()
        assert app.selected_view().state == SessionState.NEEDS_INPUT
        await pilot.press("d")
        await pilot.pause(0.3)
        assert store.sessions[SID2].reviewed_at != ""
        assert app.snapshot.by_id(SID2).state == SessionState.DONE


async def test_selection_survives_refresh(world):
    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        selected = app.selected_session_id
        app.refresh_data()
        await pilot.pause(0.2)
        assert app.selected_session_id == selected


async def test_help_modal(world):
    app, *_ = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        from cagents.modals import HelpModal

        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


async def test_empty_store_shows_hint(claude_dir, tmp_path):
    store = Store.load(tmp_path / "state.json")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        grouped = app.query_one("#grouped-list", SessionList)
        assert grouped.option_count == 1
        assert grouped.get_option_at_index(0).disabled


async def test_new_session_is_selected_and_previewed(world, tmp_path, monkeypatch):
    # A single keypress now does the whole thing — no modal, no Enter.
    app, store, tmux, _ = world
    launch = tmp_path / "newproj"
    launch.mkdir()
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(launch))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # somewhere else first
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.3)  # spawn + refresh + pending highlight
        _, new_id = tmux.shell_created[-1]
        # the list highlight moved to the new session -> it drives the preview
        assert app.selected_session_id == new_id
        listing = app.query_one(f"#{app.active_view_id}-list", SessionList)
        assert listing.highlighted_session_id == new_id
        # ...and it survives another refresh (highlight is real, not transient)
        app.refresh_data()
        await pilot.pause(0.2)
        assert app.selected_session_id == new_id


async def test_new_session_shows_needs_input_until_claude_actually_starts(
    world, tmp_path, monkeypatch
):
    # Tracked before any transcript exists — must read as "waiting on
    # you", not "something went wrong" (STOPPED), while the grace window
    # holds.
    app, store, tmux, _ = world
    launch = tmp_path / "newproj"
    launch.mkdir()
    monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(launch))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.3)
        _, new_id = tmux.shell_created[-1]
        assert app.snapshot.by_id(new_id).state == SessionState.NEEDS_INPUT


async def test_open_link_prompts_to_associate_pr(world):
    app, store, tmux, _ = world
    opened = []
    app._open_url = lambda url, label: opened.append(url)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID1)  # no links recorded
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        from cagents.modals import InputModal

        assert isinstance(app.screen, InputModal)  # prompted instead of warning
        await pilot.press(*"https://github.com/o/r/pull/5")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert store.sessions[SID1].pr_url == "https://github.com/o/r/pull/5"
        assert opened == ["https://github.com/o/r/pull/5"]  # opened right away
        # next time, o opens it directly — no prompt
        await pilot.press("o")
        await pilot.pause()
        assert not isinstance(app.screen, InputModal)
        assert opened[-1] == "https://github.com/o/r/pull/5"


class TestUndo:
    async def test_clicking_the_toast_undoes_the_action(self, world):
        from textual.widgets._toast import Toast

        app, store, tmux, _ = world
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            await pilot.pause()
            store.set_setting("notifications", True)
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("d")  # mark done -> shows an undoable toast
            await pilot.pause(0.2)
            assert store.sessions[SID1].reviewed_at != ""
            toasts = list(app.query(Toast))
            assert toasts, "expected an undoable toast after 'd'"
            plain = toasts[0].render().plain
            click_col = plain.index("click to undo") + 2  # inside the link text
            # row 1: Toast has 1 cell of padding above the text (DEFAULT_CSS)
            await pilot.click(Toast, offset=(click_col, 1))
            await pilot.pause(0.2)
            assert store.sessions[SID1].reviewed_at == ""

    async def test_undo_done_and_untrack_in_order(self, world):
        app, store, tmux, _ = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("d")  # mark done
            await pilot.pause(0.1)
            assert store.sessions[SID1].reviewed_at != ""
            select_session(app, SID3)
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("y")  # untrack SID3
            await pilot.pause(0.1)
            assert SID3 not in store.sessions

            await pilot.press("z")  # undo untrack -> SID3 restored
            await pilot.pause(0.2)
            assert SID3 in store.sessions
            assert store.sessions[SID1].reviewed_at != ""  # earlier action intact

            await pilot.press("z")  # undo done -> SID1 back to review
            await pilot.pause(0.2)
            assert store.sessions[SID1].reviewed_at == ""

            await pilot.press("z")  # stack empty -> loud no-op
            await pilot.pause(0.1)
            assert SID1 in store.sessions and SID3 in store.sessions

    async def test_undo_persists_to_disk(self, world):
        app, store, tmux, _ = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            await pilot.press(*"renamed")
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert store.sessions[SID1].label == "renamed"
            await pilot.press("z")
            await pilot.pause(0.1)
            assert store.sessions[SID1].label == ""
            from cagents.store import Store

            assert Store.load(store.path).sessions[SID1].label == ""

    async def test_undo_new_session_untracks_but_never_kills(self, world, tmp_path, monkeypatch):
        app, store, tmux, _ = world
        launch = tmp_path / "p"
        launch.mkdir()
        monkeypatch.setenv("CAGENTS_LAUNCH_CWD", str(launch))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause(0.3)
            _, new_id = tmux.shell_created[-1]
            assert new_id in store.sessions
            sessions_before = len(tmux.sessions)
            await pilot.press("z")
            await pilot.pause(0.2)
            assert new_id not in store.sessions  # bookkeeping undone
            assert len(tmux.sessions) == sessions_before  # the terminal lives on


async def test_review_queue_orders_newest_response_first(world):
    """Within the needs-review rank, the most recent response sits on top —
    even right after an app restart, when every session's in-memory
    rank_stable_since is identical (the bug: the queue degraded to project
    order)."""
    from cagents.views import attention_sort_key

    app, store, tmux, _claude_dir = world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        review = [v for v in app.snapshot.views if v.state.value == "needs review"]
        if len(review) < 2:
            import pytest

            pytest.skip("world fixture no longer has two review sessions")
        from datetime import datetime, timezone

        # Simulate post-restart: identical state-entry stamps, and give the
        # transcripts distinct finish times (older first here).
        for i, v in enumerate(review):
            v.rank_stable_since = 1000.0
            v.parsed.last_timestamp = datetime(
                2026, 8, 17, 10, i, tzinfo=timezone.utc
            )
        ordered = sorted(review, key=attention_sort_key)
        stamps = [v.last_activity for v in ordered]
        assert stamps == sorted(stamps, reverse=True), (
            "review items must be a queue: newest response first"
        )
        assert stamps[0] != stamps[-1]


async def test_state_invariant_violation_is_logged_and_surfaced(world, monkeypatch):
    """A session flipping from DONE back to NEEDS_REVIEW with no new
    transcript activity is a state-derivation bug, not a legitimate
    transition — it must be caught loudly (log + toast) the moment it
    happens, not just when someone notices the wrong label live."""
    from cagents import ctx as ctx_module

    app, store, tmux, _claude_dir = world
    logged = []
    monkeypatch.setattr(ctx_module, "_log", lambda message: logged.append(message))
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await pilot.pause()
        view = app.snapshot.views[0]
        view.state = SessionState.DONE
        app.apply_snapshot(app.snapshot)  # establishes the baseline (prev_state, prev_last_activity)
        await pilot.pause()

        view.state = SessionState.NEEDS_REVIEW  # no new transcript activity
        app.apply_snapshot(app.snapshot)
        await pilot.pause()

        assert any("STATE-INVARIANT-VIOLATION" in m for m in logged)
        from textual.widgets._toast import Toast

        toasts = list(app.query(Toast))
        assert any("invariant violation" in t.render().plain.lower() for t in toasts)


async def test_done_to_review_backed_by_new_activity_is_not_flagged(world, monkeypatch):
    from datetime import timedelta, timezone, datetime as dt

    from cagents import ctx as ctx_module

    app, store, tmux, _claude_dir = world
    logged = []
    monkeypatch.setattr(ctx_module, "_log", lambda message: logged.append(message))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.views[0]
        view.state = SessionState.DONE
        app.apply_snapshot(app.snapshot)
        await pilot.pause()

        view.state = SessionState.NEEDS_REVIEW
        if view.parsed is not None:
            baseline = view.parsed.last_timestamp or dt.now(timezone.utc)
            view.parsed.last_timestamp = baseline + timedelta(minutes=1)
        app.apply_snapshot(app.snapshot)
        await pilot.pause()

        assert not any("STATE-INVARIANT-VIOLATION" in m for m in logged)


async def test_typing_a_message_never_trips_the_invariant_check(world, monkeypatch):
    """Real bug, confirmed live: the user typed a message (a legitimate
    NEEDS_REVIEW -> WORKING, justified by the live pane's spinner, not by
    the transcript file — which Claude Code hasn't flushed yet) and got a
    false-positive violation. Nothing -> WORKING is watched at all now;
    this must stay quiet forever, regardless of transcript timestamps."""
    from cagents import ctx as ctx_module

    app, store, tmux, _claude_dir = world
    logged = []
    monkeypatch.setattr(ctx_module, "_log", lambda message: logged.append(message))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.views[0]
        view.state = SessionState.NEEDS_REVIEW
        app.apply_snapshot(app.snapshot)
        await pilot.pause()

        view.state = SessionState.WORKING  # transcript file hasn't caught up
        app.apply_snapshot(app.snapshot)
        await pilot.pause()

        assert not any("STATE-INVARIANT-VIOLATION" in m for m in logged)


class TestArrowSettings:
    """The three settings describe one set of bindings, so the app hands
    all of them to sidecar at once — and only writes the composer probe
    for the setting that actually uses it."""

    def _applied(self, monkeypatch, tmp_path, **settings):
        from cagents import app as app_module

        store = Store.load(tmp_path / "state.json")
        for key, value in settings.items():
            store.set_setting(key, value)
        calls = []
        monkeypatch.setattr(app_module, "apply_arrow_capture",
                            lambda *a, **kw: calls.append((a, kw)))
        stub = type("Stub", (), {
            "store": store,
            "_write_composer_probe": lambda self: "/probe",
        })()
        CagentsApp._apply_arrow_settings(stub)
        return calls[0]

    def test_default_captures_the_bare_pair_with_no_probe(self, monkeypatch, tmp_path):
        args, kwargs = self._applied(monkeypatch, tmp_path)
        assert args == (True, False)      # bare captured, ⌃ not
        assert kwargs["probe"] == ""      # nothing reads the screen

    def test_ctrl_setting_captures_the_second_pair(self, monkeypatch, tmp_path):
        args, _ = self._applied(monkeypatch, tmp_path, capture_ctrl_arrows=True)
        assert args == (True, True)

    def test_composer_setting_is_what_wires_the_probe(self, monkeypatch, tmp_path):
        _, kwargs = self._applied(monkeypatch, tmp_path, composer_aware_arrows=True)
        assert kwargs["probe"] == "/probe"

    def test_both_captures_off_still_reapplies(self, monkeypatch, tmp_path):
        args, _ = self._applied(monkeypatch, tmp_path, capture_left=False)
        assert args == (False, False)     # sidecar unbinds them
async def test_search_finds_a_session_by_its_r_label(world, claude_dir):
    """End to end through the modal: an R label lives only in cagents'
    store, so the search must be handed it — typing it and pressing Enter
    has to surface the session, title shown as the label."""
    app, store, tmux, _ = world
    store.set_setting("conversation_search", True)
    store.set_label(SID1, "zebra-quest")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("slash")
        await pilot.pause()
        from cagents.modals import SearchModal

        assert isinstance(app.screen, SearchModal)
        app.screen.query_one("#query").value = "zebra-quest"
        await pilot.press("enter")
        await pilot.pause(0.5)
        titles = [r.title for r in app.screen.results]
        assert "zebra-quest" in titles
