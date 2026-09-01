"""Tests for fork (f), handoff (h), lineage (*), the diff review screen
(D), and the cagents-ctx helper behind the global C-t / C-d keys."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import (
    SID1,
    SID2,
    SID3,
    FakeTmux,
    TranscriptBuilder,
    init_git_repo,
    render_text,
    select_session,
)

from cagents.app import CagentsApp
from cagents.ctx import diff_popup_command, read_context, write_context
from cagents.diffview import DiffResult, DiffScreen, compose_review_message
from cagents.gitops import ReviewComment, WorktreeDiff, parse_unified_diff
from cagents.handoff import first_message, summary_prompt
from cagents.sessions import SessionRegistry
from cagents.store import Store
from cagents.views import SessionList


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Original work").user("go").assistant_text(
        "Phase one complete."
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


# ----------------------------------------------------------------- fork ---


async def test_fork_flow(world, monkeypatch):
    """f branches the conversation immediately — no prompt modal. A fork IS
    the conversation up to that point; what you do with it is the first
    thing you type in the session itself. Handoff is the one that has to
    ask, because it needs to know what to summarise for."""
    app, store, tmux = world
    monkeypatch.setattr(time, "sleep", lambda s: None)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause(0.4)
        directory, args, new_id = tmux.created[-1]
        assert args[0:3] == ["--resume", SID1, "--fork-session"]
        assert args[3] == "--session-id" and args[4] == new_id
        assert "--settings" in args  # state hooks ride along
        child = store.sessions[new_id]
        assert child.parent_id == SID1 and child.relation == "fork"
        assert SID1 in store.sessions  # original untouched
        # No prompt was asked for, so none is delivered...
        assert tmux.sent == []
        # ...and no label is set: a label would outrank the transcript title
        # forever, freezing the fork's name after it diverges. Until the
        # fork writes a transcript of its own it borrows the parent's name
        # rather than showing a bare id.
        assert child.label == ""
        forked = app.snapshot.by_id(new_id)
        assert forked.parsed is None  # claude writes none until first message
        assert forked.title == app.snapshot.by_id(SID1).title
        assert forked.state_detail == "forked — type to begin"


async def test_fork_needs_no_typing_and_is_undoable(world):
    """The modal used to double as the confirm step; without it, z is what
    backs a fork out (untrack only — Claude's data is never touched)."""
    app, store, tmux = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        before = set(store.sessions)
        await pilot.press("f")
        await pilot.pause(0.4)
        new_id = (set(store.sessions) - before).pop()
        await pilot.press("z")
        await pilot.pause(0.3)
        assert new_id not in store.sessions
        assert SID1 in store.sessions


# -------------------------------------------------------------- handoff ---


def test_handoff_prompts():
    prompt = summary_prompt("port it to rust")
    assert "port it to rust" in prompt and "ONLY the spec" in prompt
    message = first_message("THE SPEC", "port it to rust")
    assert message.index("THE SPEC") < message.index("Your task: port it to rust")


async def test_handoff_flow(world, monkeypatch):
    app, store, tmux = world
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class FakeHandoffRunner:
        def __init__(self):
            self.prompts = []

        def run(self, prompt):
            self.prompts.append(prompt)
            return "SPEC: we built X, next do Y."

    runner = FakeHandoffRunner()
    app._handoff_runner = lambda source_id: runner

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        await pilot.press(*"finish the API layer")
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert "finish the API layer" in runner.prompts[0]
        directory, args, new_id = tmux.created[-1]
        assert args[:2] == ["--session-id", new_id]  # fresh session, not a fork
        child = store.sessions[new_id]
        assert child.parent_id == SID1 and child.relation == "handoff"
        assert store.sessions[SID1].reviewed_at != ""  # old one marked done
        sent_text = tmux.sent[0][1]
        assert "SPEC: we built X" in sent_text
        assert "Your task: finish the API layer" in sent_text


async def test_handoff_placeholder_appears_then_resolves(world, monkeypatch):
    """The list shows 'creating handoff from: ...' the moment the handoff
    starts (the spec turn can take a minute), and the row disappears when
    the real successor session takes its place."""
    import threading

    app, store, tmux = world
    monkeypatch.setattr(time, "sleep", lambda s: None)
    gate = threading.Event()

    class SlowRunner:
        def run(self, prompt):
            gate.wait(timeout=10)
            return "SPEC: we built X, next do Y."

    app._handoff_runner = lambda source_id: SlowRunner()

    def list_rows():
        session_list = app.query_one(f"#{app.active_view_id}-list", SessionList)
        return [
            (render_text(session_list.get_option_at_index(i).prompt),
             session_list.get_option_at_index(i).disabled)
            for i in range(session_list.option_count)
        ]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        await pilot.press(*"next phase")
        await pilot.press("enter")
        await pilot.pause(0.2)

        # placeholder is in the list immediately, at the top, not selectable
        rows = list_rows()
        pending = [i for i, (text, _) in enumerate(rows)
                   if "creating handoff from: Original work" in text]
        assert pending, f"no placeholder row: {rows}"
        assert rows[pending[0]][1], "placeholder must be disabled (not selectable)"
        assert tmux.created == []  # the real session doesn't exist yet

        # it survives a background refresh
        app.apply_snapshot(app.registry.refresh())
        await pilot.pause()
        assert any("creating handoff from" in text for text, _ in list_rows())

        # spec finishes: the placeholder resolves into the real session
        gate.set()
        await pilot.pause(0.5)
        rows = list_rows()
        assert not any("creating handoff from" in text for text, _ in rows), rows
        assert tmux.created, "successor session should have been created"


async def test_handoff_placeholder_clears_on_failure(world, monkeypatch):
    app, store, tmux = world
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class FailingRunner:
        def run(self, prompt):
            raise RuntimeError("claude exploded")

    app._handoff_runner = lambda source_id: FailingRunner()

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        await pilot.press(*"x")
        await pilot.press("enter")
        await pilot.pause(0.5)

        session_list = app.query_one(f"#{app.active_view_id}-list", SessionList)
        rows = [render_text(session_list.get_option_at_index(i).prompt)
                for i in range(session_list.option_count)]
        assert not any("creating handoff from" in r for r in rows), rows
        assert tmux.created == []


async def test_handoff_empty_spec_aborts(world, monkeypatch):
    app, store, tmux = world
    app._handoff_runner = lambda sid: type("R", (), {"run": lambda self, p: "  "})()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        await pilot.press(*"x")
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert tmux.created == []
        assert store.sessions[SID1].reviewed_at == ""


# -------------------------------------------------------------- lineage ---


@pytest.fixture
def family(claude_dir: Path, tmp_path: Path, now: float):
    for sid, title in ((SID1, "Parent"), (SID2, "Fork A"), (SID3, "Fork B")):
        TranscriptBuilder(sid, "/proj/alpha").ai_title(title).user("go").assistant_text(
            "done"
        ).write(claude_dir, mtime=now - 500)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    store.track(SID2, "/proj/alpha", "2026-08-18T09:01:00+00:00",
                parent_id=SID1, relation="fork")
    store.track(SID3, "/proj/alpha", "2026-08-18T09:02:00+00:00",
                parent_id=SID1, relation="handoff")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store


def test_lineage_resolved_in_snapshot(family, now):
    app, store = family
    snap = app.registry.refresh(now=now)
    parent = snap.by_id(SID1)
    fork_a = snap.by_id(SID2)
    assert sorted(parent.child_ids) == sorted([SID2, SID3])
    assert fork_a.parent_id == SID1 and fork_a.relation == "fork"
    assert fork_a.sibling_ids == [SID3]


async def test_related_modal_lists_and_jumps(family):
    app, store = family
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        select_session(app, SID2)
        await pilot.pause()
        await pilot.press("asterisk")
        await pilot.pause()
        from cagents.modals import RelatedModal

        assert isinstance(app.screen, RelatedModal)
        listing = app.screen.query_one("#related-list")
        rows = "\n".join(
            render_text(listing.get_option_at_index(i).prompt)
            for i in range(listing.option_count)
        )
        assert "parent (fork)" in rows and "Parent" in rows
        assert "sibling" in rows and "Fork B" in rows
        await pilot.press("enter")  # choose the parent
        await pilot.pause(0.2)
        assert app.selected_session_id == SID1


# ---------------------------------------------------------- diff screen ---

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
    return WorktreeDiff(
        directory="/proj/alpha", branch="feat", base="main",
        lines=parse_unified_diff(SAMPLE_DIFF), files=["src/x.py"],
        additions=2, deletions=1,
    )


def test_compose_review_message():
    comments = [
        ReviewComment("src/x.py", 11, "rename this", "you", source="local"),
        ReviewComment("", 0, "[APPROVED] nice", "octocat", source="github"),
    ]
    message = compose_review_message(_diff(), comments)
    assert "feat vs main" in message
    assert "1. src/x.py:11 — rename this" in message
    assert "2. (overall) [from octocat on GitHub] — [APPROVED] nice" in message


async def test_diff_screen_comment_and_send(world):
    app, store, tmux = world
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        results: list = []
        app.push_screen(DiffScreen(_diff(), "'Alpha'"), results.append)
        await pilot.pause()
        assert isinstance(app.screen, DiffScreen)
        await pilot.press("j")
        await pilot.press("c")
        await pilot.pause()
        await pilot.press(*"tighten this up")
        await pilot.press("enter")
        await pilot.pause()
        listing = app.screen.query_one("#diff-list")
        rendered = "\n".join(
            render_text(listing.get_option_at_index(i).prompt)
            for i in range(listing.option_count)
        )
        assert "you: tighten this up" in rendered
        await pilot.press("s")
        await pilot.pause()
        assert results and isinstance(results[0], DiffResult)
        assert "tighten this up" in results[0].send_message


async def test_send_review_resumes_dead_session(world, monkeypatch):
    app, store, tmux = world
    monkeypatch.setattr(time, "sleep", lambda s: None)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        view = app.snapshot.by_id(SID1)
        assert not view.live
        app._diff_closed(SID1, DiffResult(send_message="comments here", comment_count=2))
        await pilot.pause(0.4)
        assert tmux.created and tmux.created[-1][1][:2] == ["--resume", SID1]
        assert tmux.sent and tmux.sent[0][1] == "comments here"
        assert tmux.sent[0][2] == "cagents-sessions"


# ------------------------------------------------------------------ ctx ---


class TestCtx:
    def test_context_roundtrip(self, tmp_path):
        path = tmp_path / "context.json"
        write_context(
            path, "/proj/x", SID1, diff_mode="uncommitted", shim_dir="/data/bin",
            tmux_name="alpha", tmux_socket="claude",
        )
        assert read_context(path) == {
            "dir": "/proj/x", "session_id": SID1, "diff_mode": "uncommitted",
            "shim_dir": "/data/bin", "tmux_name": "alpha", "tmux_socket": "claude",
        }
        assert read_context(tmp_path / "missing.json") == {}

    def test_diff_popup_command_shape(self):
        command = diff_popup_command("/proj/with space")
        assert "'/proj/with space'" in command
        assert "merge-base" in command
        assert "git diff --color" in command
        assert "less -R" in command  # pager; q closes the popup

    def test_diff_popup_command_prefers_delta_falls_back_to_cat(self):
        # Prettier when delta (git-delta) is installed, unchanged
        # plain-colored output when it isn't — never a hard dependency.
        command = diff_popup_command("/proj/x")
        assert "command -v delta" in command
        assert "delta --line-numbers" in command
        assert "|| cat" in command

    def test_do_shell_guards_missing_dir(self, monkeypatch):
        from cagents import ctx

        monkeypatch.setattr(ctx, "_workspace_alive", lambda: False)
        monkeypatch.setattr(ctx, "_tmux", lambda *a: 0)
        monkeypatch.setattr(ctx, "_display", lambda m: None)
        assert ctx.do_shell("") == 1
        assert ctx.do_shell("/definitely/not/a/dir") == 1

    def test_resolve_terminal_directory_distinguishes_worktree_kinds(self, tmp_path):
        from cagents.ctx import resolve_terminal_directory

        # not a real directory at all, or not a git repo -> nothing usable
        assert resolve_terminal_directory("") == ("", "", "")
        plain = tmp_path / "plain"
        plain.mkdir()
        assert resolve_terminal_directory(str(plain)) == ("", "", "")
        # a real repo checkout, but no dedicated worktree -> usable with a warning
        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        effective, kind, warning = resolve_terminal_directory(str(repo))
        assert kind == "main" and Path(effective).resolve() == repo.resolve()
        assert "no dedicated worktree" in warning
        # a real linked worktree -> usable, no warning
        wt = tmp_path / "wt"
        subprocess.run(
            ["git", "worktree", "add", "-b", "feature", str(wt)],
            cwd=repo, capture_output=True, check=True,
        )
        effective, kind, warning = resolve_terminal_directory(str(wt))
        assert (effective, kind, warning) == (str(wt), "linked", "")

    def test_do_shell_errors_without_falling_back_when_no_worktree(self, monkeypatch, tmp_path):
        from cagents import ctx

        monkeypatch.setattr(ctx, "_workspace_alive", lambda: True)
        respawned = []
        monkeypatch.setattr(ctx, "_work", lambda *a: respawned.append(a) or 0)
        displayed = []
        monkeypatch.setattr(ctx, "_display", lambda msg: displayed.append(msg))
        plain = tmp_path / "plain"
        plain.mkdir()
        assert ctx.do_shell(str(plain)) == 1
        respawn = next(c for c in respawned if c[0] == "respawn-pane")
        assert "no git worktree found" in " ".join(str(a) for a in respawn)
        assert displayed == []  # quiet by design: the pane text IS the feedback
        # never silently falls back to a plain shell anywhere else
        assert not any(c[0] == "new-window" for c in respawned)

    def test_do_shell_warns_but_still_opens_in_the_shared_repo_checkout(
        self, monkeypatch, tmp_path
    ):
        from cagents import ctx

        repo = tmp_path / "repo"
        repo.mkdir()
        init_git_repo(repo)
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: True)
        monkeypatch.setattr(ctx, "_work_windows", lambda: ["session", "diff", "term-1"])
        work_calls = []
        monkeypatch.setattr(ctx, "_work", lambda *a: work_calls.append(a) or 0)
        displayed = []
        monkeypatch.setattr(ctx, "_display", lambda msg: displayed.append(msg))
        assert ctx.do_shell(str(repo)) == 0
        assert displayed == []  # shared checkout opens silently (user choice)
        respawn = next(c for c in work_calls if c[0] == "respawn-pane")
        assert str(repo.resolve()) in " ".join(str(a) for a in respawn)

    def test_do_shell_scopes_the_terminal_to_the_live_sessions_own_window(
        self, monkeypatch, tmp_path
    ):
        from cagents import ctx

        wt = tmp_path / "wt"
        wt.mkdir()
        init_git_repo(wt)
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: True)
        monkeypatch.setattr(ctx, "_work_windows", lambda: ["session", "diff", "term-1"])
        work_calls = []
        last_target = {"value": ""}
        monkeypatch.setattr(ctx, "_work", lambda *a: work_calls.append(a) or 0)
        monkeypatch.setattr(ctx, "_last_term_target", lambda: last_target["value"])

        def fake_set(value):
            last_target["value"] = value

        monkeypatch.setattr(ctx, "_set_last_term_target", fake_set)
        ensure_calls = []

        class FakeClient:
            def ensure_session_window(self, name, window, directory, socket=None):
                ensure_calls.append(("window", name, window, directory, socket))

            def ensure_window_view(self, name, window, socket=None, force_select=False):
                ensure_calls.append(("view", name, window, socket))
                return f"{name}--{window}"

        monkeypatch.setattr("cagents.tmuxctl.TmuxClient", FakeClient)
        assert ctx.do_shell(str(wt), tmux_name="alpha", tmux_socket="claude") == 0
        assert ("window", "alpha", "term", str(wt), "claude") in ensure_calls
        assert ("view", "alpha", "term", "claude") in ensure_calls
        respawn = next(c for c in work_calls if c[0] == "respawn-pane")
        assert "alpha--term" in " ".join(str(a) for a in respawn)

        # asking again for the SAME session's terminal must not respawn
        # (would kill whatever's running in the shell already)
        work_calls.clear()
        ctx.do_shell(str(wt), tmux_name="alpha", tmux_socket="claude")
        assert not any(c[0] == "respawn-pane" for c in work_calls)

    def test_lazygit_command_cds_and_uses_the_disable_popups_config(self, tmp_path, monkeypatch):
        from cagents import ctx

        monkeypatch.setattr(ctx, "LAZYGIT_CONFIG_DIR", tmp_path)
        command = ctx.lazygit_command("/proj/with space")
        assert "cd '/proj/with space'" in command
        assert "lazygit --use-config-file=" in command
        assert (tmp_path / "lazygit.yml").read_text() == "disableStartupPopups: true\n"
        # idempotent: doesn't clobber a config that already exists
        (tmp_path / "lazygit.yml").write_text("disableStartupPopups: true\nfoo: bar\n")
        ctx.lazygit_command("/proj/x")
        assert "foo: bar" in (tmp_path / "lazygit.yml").read_text()

    def test_merge_base_ref_empty_without_a_default_branch(self, monkeypatch):
        from cagents import ctx

        monkeypatch.setattr("cagents.gitops.default_branch", lambda directory: "")
        assert ctx._merge_base_ref("/proj/x") == ""

    def test_do_diff_prefers_lazygit_when_installed(self, monkeypatch, tmp_path):
        from cagents import ctx

        monkeypatch.setattr(ctx, "LAZYGIT_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/lazygit" if name == "lazygit" else None)
        monkeypatch.setattr(
            subprocess, "run",
            lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: False)
        calls = []
        monkeypatch.setattr(ctx, "_tmux", lambda *a: calls.append(a) or 0)
        ctx.do_diff(str(tmp_path), mode="uncommitted")
        assert any("lazygit" in " ".join(c) for c in calls)

    def test_do_diff_falls_back_to_pager_when_lazygit_missing(self, monkeypatch, tmp_path):
        from cagents import ctx

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(
            subprocess, "run",
            lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: False)
        calls = []
        monkeypatch.setattr(ctx, "_tmux", lambda *a: calls.append(a) or 0)
        ctx.do_diff(str(tmp_path), mode="uncommitted")
        assert any("less -R" in " ".join(c) for c in calls)
        assert not any("lazygit" in " ".join(c) for c in calls)


class TestShellPick:
    def test_completion_returns_matches_for_cycling(self, tmp_path):
        from cagents.modals import complete_directory

        (tmp_path / "proj-alpha").mkdir()
        (tmp_path / "proj-beta").mkdir()
        (tmp_path / ".hidden-proj").mkdir()
        completed, matches = complete_directory(str(tmp_path / "proj"))
        assert completed == str(tmp_path / "proj-")  # common prefix
        assert len(matches) == 2  # dotdirs hidden for a non-dot fragment
        completed, matches = complete_directory(str(tmp_path / ".hid"))
        assert len(matches) == 1 and completed.endswith("/")

    async def test_pick_directory_via_shell_reads_final_cwd(self, claude_dir, tmp_path, now):
        from conftest import FakeTmux

        target = tmp_path / "picked-here"
        target.mkdir()
        store = Store.load(tmp_path / "state.json")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        app = CagentsApp(store=store, registry=registry, tmux=FakeTmux(), claude_dir=claude_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # a scripted "interactive shell": cd somewhere, linger, exit
            picked = app.pick_directory_via_shell(
                str(tmp_path),
                shell_cmd=["/bin/sh", "-c", f"cd '{target}' && sleep 0.7"],
            )
            assert picked == str(target)


class TestShellClaude:
    """Typing `claude` in a cagents shell pulls the session into cagents."""

    async def test_spawn_request_flow(self, world, tmp_path):
        app, store, tmux = world
        project = tmp_path / "typedhere"
        project.mkdir()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            import json

            app._spawn_request_path().write_text(
                json.dumps({"dir": str(project), "args": []})
            )
            app.apply_snapshot(app.registry.refresh())
            await pilot.pause(0.3)
            # spawned with a fresh session id + hooks, in the typed cwd
            directory, args, sid = tmux.created[-1]
            assert directory == str(project)
            assert "--session-id" in args and "--settings" in args
            assert sid in store.sessions
            assert app.selected_session_id == sid
            assert not app._spawn_request_path().exists()  # consumed

    async def test_spawn_request_resume_keeps_id(self, world, tmp_path):
        app, store, tmux = world
        project = tmp_path / "resumehere"
        project.mkdir()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            import json

            app._spawn_request_path().write_text(
                json.dumps({"dir": str(project), "args": ["--resume", SID2]})
            )
            app.apply_snapshot(app.registry.refresh())
            await pilot.pause(0.3)
            directory, args, sid = tmux.created[-1]
            assert sid == SID2  # explicit resume keeps its identity
            assert "--session-id" not in args
            assert SID2 in store.sessions

    async def test_spawn_request_reuses_a_pending_new_terminal_id(self, world, tmp_path):
        # `n` tracks a session before `claude` is ever typed — once the
        # shim reports one, its exact id must be reused (already tracked,
        # the list row is waiting on it) rather than minting a second,
        # orphaned one.
        app, store, tmux = world
        project = tmp_path / "pendinghere"
        project.mkdir()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            import json

            pending_id = "99999999-9999-9999-9999-999999999999"
            app._pending_new_terminals.add(pending_id)
            store.track(pending_id, str(project), "2026-08-17T09:00:00+00:00")
            app._spawn_request_path().write_text(
                json.dumps({"dir": str(project), "pending_id": pending_id, "args": []})
            )
            app.apply_snapshot(app.registry.refresh())
            await pilot.pause(0.3)
            directory, args, sid = tmux.created[-1]
            assert sid == pending_id
            assert "--session-id" in args
            assert pending_id not in app._pending_new_terminals  # consumed

    async def test_spawn_request_ignores_an_unrelated_cagents_session_id(self, world, tmp_path):
        # The env var a spawn request's pending_id rides on is ALSO present
        # for a shell opened via the existing "N" terminal-tab-on-a-live-
        # session feature — but that id is already a real, spawned
        # session, never in _pending_new_terminals, so it must still get a
        # genuinely fresh id (today's behavior, unchanged).
        app, store, tmux = world
        project = tmp_path / "elsewhere"
        project.mkdir()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            import json

            live_id = "88888888-8888-8888-8888-888888888888"
            store.track(live_id, str(project), "2026-08-17T09:00:00+00:00")
            app._spawn_request_path().write_text(
                json.dumps({"dir": str(project), "pending_id": live_id, "args": []})
            )
            app.apply_snapshot(app.registry.refresh())
            await pilot.pause(0.3)
            directory, args, sid = tmux.created[-1]
            assert sid != live_id

    async def test_bad_directory_is_loud_and_consumed(self, world):
        app, store, tmux = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            import json

            app._spawn_request_path().write_text(
                json.dumps({"dir": "/not/a/real/dir", "args": []})
            )
            app.apply_snapshot(app.registry.refresh())
            await pilot.pause(0.2)
            assert tmux.created == []
            assert not app._spawn_request_path().exists()

    async def test_shim_script_contents(self, world):
        app, store, tmux = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            shim = app._shim_dir() / "claude"
            assert shim.exists()
            import os as _os

            assert _os.access(shim, _os.X_OK)
            script = shim.read_text()
            assert str(app._spawn_request_path()) in script
            assert "exec" in script and "not responding" in script  # fallback path


async def test_shift_n_opens_terminal_tab(world, tmp_path, now, claude_dir):
    from conftest import FakeOuterTmux, FakeWorkTmux
    from cagents.sidecar import Sidecar
    from cagents.tmuxctl import TmuxSession

    app, store, tmux = world
    # The terminal belongs to a live session's own worktree — needs one
    # that's actually live, on a directory that actually exists. work_dir
    # comes from the transcript's own recorded cwd, not tracked.project_dir
    # — rewrite the transcript so it resolves to a real directory.
    real_dir = tmp_path / "alpha-worktree"
    real_dir.mkdir()
    init_git_repo(real_dir)
    TranscriptBuilder(SID1, str(real_dir)).ai_title("Original work").user("go").assistant_text(
        "Phase one complete."
    ).write(claude_dir, mtime=now - 1)
    store.sessions[SID1].project_dir = str(real_dir)
    tmux.sessions.append(
        TmuxSession(name="alpha", created=now - 60, activity=now, attached=False,
                    pane_pid=1, pane_path=str(real_dir), socket="claude")
    )
    outer, work = FakeOuterTmux(), FakeWorkTmux()
    app.sidecar = Sidecar(runner=outer, own_pane="%0", work_runner=work)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        select_session(app, SID1)
        await pilot.pause(0.5)
        work.calls.clear()
        await pilot.press("N")
        await pilot.pause()
        assert ["select-window", "-t", "=work:term-1"] in work.calls
        assert ["select-pane", "-t", "%1"] in outer.calls  # pane focused, tab untouched
        assert any(entry.startswith("ensure-window:alpha:term:") for entry in tmux.log)


class TestCtxToasts:
    """cagents-ctx runs as a short-lived tmux hook process — its warnings
    must reach the app as real toasts via the toast-request file, not just
    a tmux display-message flash."""

    def test_do_shell_shared_checkout_is_silent(self, monkeypatch, tmp_path):
        import subprocess

        from cagents import ctx

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: True)
        monkeypatch.setattr(ctx, "_work_windows", lambda: ["session", "diff", "term-1"])
        monkeypatch.setattr(ctx, "_work", lambda *a: 0)
        monkeypatch.setattr(ctx, "_tmux", lambda *a: 0)
        monkeypatch.setattr(ctx, "_display", lambda msg: None)
        monkeypatch.setattr(ctx, "_last_term_target", lambda: "")
        monkeypatch.setattr(ctx, "_set_last_term_target", lambda v: None)

        assert ctx.do_shell(str(repo), state_dir=state) == 0
        # quiet by design (user choice): shared checkout queues NO toast
        assert not (state / ctx.TOAST_REQUEST_FILE).exists()

    def test_do_shell_no_worktree_is_silent(self, monkeypatch, tmp_path):
        import json

        from cagents import ctx

        plain = tmp_path / "plain"
        plain.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: True)
        monkeypatch.setattr(ctx, "_work", lambda *a: 0)
        monkeypatch.setattr(ctx, "_tmux", lambda *a: 0)
        monkeypatch.setattr(ctx, "_display", lambda msg: None)
        monkeypatch.setattr(ctx, "_set_last_term_target", lambda v: None)

        assert ctx.do_shell(str(plain), state_dir=state) == 1
        # quiet by design (user choice): no toast — the pane placeholder explains
        assert not (state / ctx.TOAST_REQUEST_FILE).exists()

    async def test_app_drains_toast_requests_into_notify(self, world, monkeypatch):
        import json

        from cagents.ctx import TOAST_REQUEST_FILE

        app, store, tmux = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            shown = []
            monkeypatch.setattr(
                app, "notify",
                lambda message, **kw: shown.append((message, kw.get("severity"))),
            )
            path = store.path.parent / TOAST_REQUEST_FILE
            path.write_text(
                json.dumps({"message": "cagents: no dedicated worktree for this session — "
                                       "terminal opened in the shared repo checkout (/r)",
                            "severity": "warning"}) + "\n"
                + "not json\n"
                + json.dumps({"message": "", "severity": "warning"}) + "\n"
            )
            app._handle_toast_requests()
            assert len(shown) == 1 and shown[0][1] == "warning"
            assert "no dedicated worktree" in shown[0][0]
            assert not path.exists()  # consumed
            app._handle_toast_requests()  # empty queue: no crash, no repeats
            assert len(shown) == 1


class TestCtxLogging:
    def test_tmux_entry_never_exits_nonzero_and_logs(self, monkeypatch, tmp_path):
        """A nonzero exit from a tmux run-shell hook paints a raw
        'returned N' screen over the user's pane — tmux_entry must swallow
        every failure into ctx.log instead."""
        import json

        from cagents import ctx

        context = tmp_path / "context.json"
        # dir points nowhere usable -> do_shell's error path (exit 1 inside)
        context.write_text(json.dumps({"dir": str(tmp_path / "not-a-repo")}))
        (tmp_path / "not-a-repo").mkdir()
        monkeypatch.setattr(ctx, "_workspace_alive", lambda: True)
        monkeypatch.setattr(ctx, "_work", lambda *a: 0)
        monkeypatch.setattr(ctx, "_tmux", lambda *a: 0)
        monkeypatch.setattr(ctx, "_set_last_term_target", lambda v: None)

        assert ctx.tmux_entry(["shell", "--context", str(context)]) == 0
        log = (tmp_path / ctx.LOG_FILE_NAME).read_text()
        assert "invoked (" in log and "do_shell:" in log and "exit: 1" in log
        # quiet by design: worktree problems never toast (the pane explains)
        assert not (tmp_path / ctx.TOAST_REQUEST_FILE).exists()

    def test_tmux_entry_logs_crashes(self, monkeypatch, tmp_path):
        import json

        from cagents import ctx

        context = tmp_path / "context.json"
        context.write_text(json.dumps({"dir": "/x"}))
        monkeypatch.setattr(
            ctx, "do_shell",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert ctx.tmux_entry(["shell", "--context", str(context)]) == 0
        assert "boom" in (tmp_path / ctx.LOG_FILE_NAME).read_text()


    def test_invocation_logs_version_and_commit(self, monkeypatch, tmp_path):
        import json

        from cagents import ctx

        context = tmp_path / "context.json"
        context.write_text(json.dumps({"dir": "/x"}))
        monkeypatch.setattr(ctx, "do_shell", lambda *a, **k: 0)
        ctx.tmux_entry(["shell", "--context", str(context)])
        log = (tmp_path / ctx.LOG_FILE_NAME).read_text()
        assert "invoked (cagents " in log
        assert "@" in log  # commit hash resolved (editable install from the repo)
