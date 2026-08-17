"""Tests for the todo view/store and the diff review screen."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import SID1, SID2, TranscriptBuilder
from test_app import FakeTmux, render_text, widget_text

from cagents.app import CagentsApp
from cagents.diffview import DiffResult, DiffScreen, compose_review_message
from cagents.gitops import ReviewComment, WorktreeDiff, parse_unified_diff, worktree_diff
from cagents.sessions import SessionRegistry
from cagents.store import Store
from cagents.views import SessionList, TodoView


# ------------------------------------------------------------- store ------


class TestTodoStore:
    def test_roundtrip(self, tmp_path: Path):
        path = tmp_path / "state.json"
        store = Store.load(path)
        todo = store.add_todo("fix auth", "2026-08-17T09:00:00+00:00", project_dir="/proj/a")
        store.link_todo_session(todo.todo_id, SID1)
        store.link_todo_session(todo.todo_id, SID1)  # dedupe
        store.set_todo_worktree(todo.todo_id, "/proj/a-worktrees/fix-auth")

        reloaded = Store.load(path)
        got = reloaded.todos[todo.todo_id]
        assert got.text == "fix auth"
        assert got.session_ids == [SID1]
        assert got.worktree == "/proj/a-worktrees/fix-auth"
        assert not got.done

        store.set_todo_done(todo.todo_id, "2026-08-17T12:00:00+00:00")
        assert Store.load(path).todos[todo.todo_id].done
        store.set_todo_done(todo.todo_id, "")
        assert not Store.load(path).todos[todo.todo_id].done
        store.delete_todo(todo.todo_id)
        assert Store.load(path).todos == {}

    def test_archived_sessions_hidden_from_refresh(self, claude_dir, tmp_path, now):
        TranscriptBuilder(SID1, "/proj/a").user("x").assistant_text("y").write(
            claude_dir, mtime=now - 100
        )
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, "/proj/a", "2026-08-17T09:00:00+00:00")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        assert len(registry.refresh(now=now).views) == 1
        store.set_archived(SID1, True)
        assert registry.refresh(now=now).views == []
        store.set_archived(SID1, False)
        assert len(registry.refresh(now=now).views) == 1


# ------------------------------------------------------------- UI world ---


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha: fix auth").user("go").assistant_text(
        "done"
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-17T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


async def test_todo_view_add_and_list(world):
    app, store, _ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        assert app.active_view_id == "todos"
        await pilot.press("A")
        await pilot.pause()
        await pilot.press(*"write more tests")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert any(t.text == "write more tests" for t in store.todos.values())
        todos_list = app.query_one("#todos-list", SessionList)
        text = render_text(todos_list.get_option_at_index(1).prompt)
        assert "write more tests" in text


async def test_todo_new_session_links(world, tmp_path):
    app, store, tmux = world
    project = tmp_path / "todoproj"
    project.mkdir()
    todo = store.add_todo("build the thing", "2026-08-17T09:00:00+00:00", str(project))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.press("n")  # new session for selected todo
        await pilot.pause()
        dir_input = app.screen.query_one("#dir")
        assert dir_input.value == str(project)  # prefilled from the todo
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert tmux.created
        _, args, sid = tmux.created[-1]
        assert store.todos[todo.todo_id].session_ids == [sid]


async def test_todo_done_archives_workspace(world, claude_dir, now):
    app, store, tmux = world
    todo = store.add_todo("auth work", "2026-08-17T09:00:00+00:00", "/proj/alpha")
    store.link_todo_session(todo.todo_id, SID1)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")  # yes, archive
        await pilot.pause(0.3)
        assert store.todos[todo.todo_id].done
        assert store.sessions[SID1].archived is True
        assert app.snapshot.views == []  # hidden from session views

        # Reopen -> unarchive
        await pilot.press("d")
        await pilot.pause(0.3)
        assert not store.todos[todo.todo_id].done
        assert store.sessions[SID1].archived is False


async def test_todo_delete_with_confirm(world):
    app, store, _ = world
    todo = store.add_todo("obsolete", "2026-08-17T09:00:00+00:00")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause(0.2)
        assert todo.todo_id not in store.todos


async def test_todo_worktree_creates_session(world, tmp_path):
    app, store, tmux = world
    repo = tmp_path / "gitproj"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n", "utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)

    todo = store.add_todo("try worktrees", "2026-08-17T09:00:00+00:00", str(repo))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.press("W")
        await pilot.pause(1.0)  # worktree worker
        got = store.todos[todo.todo_id]
        assert got.worktree.endswith("gitproj-worktrees/try-worktrees")
        assert Path(got.worktree).is_dir()
        assert len(got.session_ids) == 1
        assert tmux.created and tmux.created[-1][0] == got.worktree


# ------------------------------------------------------------ diff screen --

SAMPLE_DIFF = """\
diff --git a/src/x.py b/src/x.py
index 111..222 100644
--- a/src/x.py
+++ b/src/x.py
@@ -10,3 +10,4 @@ def f():
 context line
-old line
+new line
+added line
"""


def _diff() -> WorktreeDiff:
    lines = parse_unified_diff(SAMPLE_DIFF)
    return WorktreeDiff(
        directory="/proj/alpha", branch="todo/x", base="main", lines=lines,
        files=["src/x.py"], additions=2, deletions=1,
    )


def test_compose_review_message():
    comments = [
        ReviewComment("src/x.py", 11, "rename this", "you", source="local"),
        ReviewComment("", 0, "[APPROVED] nice", "octocat", source="github"),
    ]
    message = compose_review_message(_diff(), comments)
    assert "todo/x vs main" in message
    assert "1. src/x.py:11 — rename this" in message
    assert "2. (overall) [from octocat on GitHub] — [APPROVED] nice" in message


async def test_diff_screen_comment_and_send(world):
    app, store, tmux = world

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        results: list = []
        app.push_screen(DiffScreen(_diff(), "'Alpha'"), results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DiffScreen)
        # cursor starts on first selectable line; comment on it
        await pilot.press("j")  # move to a diff line
        await pilot.press("c")
        await pilot.pause()
        await pilot.press(*"tighten this up")
        await pilot.press("enter")
        await pilot.pause()
        # comment is rendered inline
        listing = app.screen.query_one("#diff-list")
        rendered = "\n".join(
            render_text(listing.get_option_at_index(i).prompt)
            for i in range(listing.option_count)
        )
        assert "you: tighten this up" in rendered
        # send
        await pilot.press("s")
        await pilot.pause()
        assert results and isinstance(results[0], DiffResult)
        assert "tighten this up" in results[0].send_message
        assert results[0].comment_count == 1


async def test_diff_screen_send_without_comments_refuses(world):
    app, *_ = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        results: list = []
        app.push_screen(DiffScreen(_diff(), "'Alpha'"), results.append)
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, DiffScreen)  # still open
        status = widget_text(app.screen, "#diff-status") if False else render_text(
            app.screen.query_one("#diff-status").content
        )
        assert "No comments yet" in status


async def test_diff_screen_pulls_github_comments(world):
    app, *_ = world
    gh_comments = [
        ReviewComment("src/x.py", 11, "please add a docstring", "octocat"),
        ReviewComment("", 0, "[CHANGES_REQUESTED] see notes", "octocat"),
    ]
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.push_screen(DiffScreen(_diff(), "'Alpha'", github_puller=lambda: gh_comments))
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause(0.3)
        screen = app.screen
        listing = screen.query_one("#diff-list")
        rendered = "\n".join(
            render_text(listing.get_option_at_index(i).prompt)
            for i in range(listing.option_count)
        )
        assert "octocat: please add a docstring" in rendered
        assert "CHANGES_REQUESTED" in rendered
        # pulling again doesn't duplicate
        await pilot.press("g")
        await pilot.pause(0.3)
        status = render_text(screen.query_one("#diff-status").content)
        assert "0 new" in status


async def test_send_review_to_live_session(world, claude_dir, now, tmp_path):
    app, store, tmux = world

    sent: list[tuple[str, str]] = []
    tmux.send_text = lambda name, text, submit=True: sent.append((name, text))
    from cagents.tmuxctl import TmuxSession

    tmux.sessions.append(
        TmuxSession(
            name="alpha", created=now - 60, activity=now, attached=False,
            pane_pid=1, pane_path="/proj/alpha",
        )
    )
    # make the session look live (recent write)
    TranscriptBuilder(SID2, "/proj/alpha").ai_title("live one").user("hi").write(
        claude_dir, mtime=now - 1
    )
    store.track(SID2, "/proj/alpha", "2026-08-17T09:00:00+00:00")

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.by_id(SID2)
        assert view.live
        app._diff_closed(SID2, DiffResult(send_message="please fix x", comment_count=1))
        await pilot.pause(0.3)
        assert sent == [("alpha", "please fix x")]


async def test_send_review_resumes_dead_session(world, monkeypatch):
    app, store, tmux = world
    sent = []
    tmux.send_text = lambda name, text, submit=True: sent.append((name, text))
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip the boot wait

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.by_id(SID1)
        assert not view.live
        app._diff_closed(SID1, DiffResult(send_message="comments here", comment_count=2))
        await pilot.pause(0.4)
        assert tmux.created and tmux.created[-1][1] == ["--resume", SID1]
        assert sent and sent[0][1] == "comments here"
