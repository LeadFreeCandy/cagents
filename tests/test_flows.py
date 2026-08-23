"""Tests for fork (f), handoff (h), lineage (*), the diff review screen
(D), and the cagents-ctx helper behind the global C-s / C-d keys."""

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
    app, store, tmux = world
    monkeypatch.setattr(time, "sleep", lambda s: None)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press(*"try the async approach")
        await pilot.press("enter")
        await pilot.pause(0.4)
        directory, args, new_id = tmux.created[-1]
        assert args[0:3] == ["--resume", SID1, "--fork-session"]
        assert args[3] == "--session-id" and args[4] == new_id
        assert "--settings" in args  # state hooks ride along
        child = store.sessions[new_id]
        assert child.label == "try the async approach"
        assert child.parent_id == SID1 and child.relation == "fork"
        assert SID1 in store.sessions  # original untouched
        # the typed prompt was delivered into the new session on the private socket
        assert tmux.sent and tmux.sent[0][1] == "try the async approach"
        assert tmux.sent[0][2] == "cagents-sessions"


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

    def test_do_shell_guards_missing_dir(self):
        from cagents.ctx import do_shell

        assert do_shell("") == 1
        assert do_shell("/definitely/not/a/dir") == 1

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
        assert any("no git worktree" in m for m in displayed)
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
        assert any("no dedicated worktree" in m for m in displayed)
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

            def ensure_window_view(self, name, window, socket=None):
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

    def test_next_term_name_fills_gaps_and_avoids_collisions(self):
        from cagents.ctx import _next_term_name

        assert _next_term_name(["session", "diff", "term-1", "+term"]) == "term-2"
        assert _next_term_name(["session", "diff", "+term"]) == "term-1"
        # term-1 was closed but term-2 and term-4 are still open — the
        # next name must not collide with a still-open higher number
        assert _next_term_name(["session", "diff", "term-2", "term-4", "+term"]) == "term-5"

    def test_do_new_term_requires_a_live_workspace(self, monkeypatch):
        from cagents import ctx

        monkeypatch.setattr(ctx, "_workspace_alive", lambda: False)
        assert ctx.do_new_term("/proj/x") == 1

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
