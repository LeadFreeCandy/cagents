"""Auto-worktree sessions: every conversation cagents starts gets its own
linked worktree, so the diff and terminal tabs act on a directory nothing
else is checked out in.

Covers the git layer against real temporary repositories, the spawn gate
that decides whether a new conversation gets one, and the two places that
have to map a worktree back to the repo it came from (grouping and the
new-conversation directory shortcuts)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import SID1, SID2, FakeTmux, TranscriptBuilder, TranscriptBuilder as TB, widget_text

from cagents.app import CagentsApp
from cagents.gitops import (
    GitError,
    base_ref,
    cagents_worktrees,
    create_worktree,
    current_branch,
    next_worktree_slot,
    owning_repo,
    remove_worktree,
    worktree_status,
)
from cagents.sessions import SessionRegistry
from cagents.store import SETTINGS_DEFAULTS, Store


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def main():\n    return 1\n", "utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def cloned(tmp_path: Path, repo: Path) -> Path:
    """A checkout with a real `origin` — the only way to tell a remote-first
    base ref apart from the local branch of the same name."""
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    return clone


# ------------------------------------------------------------- git layer ---


class TestCreateWorktree:
    def test_names_the_next_free_slot(self, repo: Path):
        first = create_worktree(str(repo))
        second = create_worktree(str(repo))

        holder = repo.parent / "repo-worktrees"
        assert Path(first) == holder / "cagents-1"
        assert Path(second) == holder / "cagents-2"
        assert current_branch(first) == "cagents/1"
        assert current_branch(second) == "cagents/2"
        assert worktree_status(first)[0] == "linked"

    def test_slot_skips_an_existing_directory_or_branch(self, repo: Path):
        (repo.parent / "repo-worktrees" / "cagents-1").mkdir(parents=True)
        _git(repo, "branch", "cagents/2")

        dest, branch = next_worktree_slot(str(repo))
        assert Path(dest).name == "cagents-3"
        assert branch == "cagents/3"

    def test_branches_off_the_default_branch_not_the_checked_out_head(self, repo: Path):
        _git(repo, "checkout", "-q", "-b", "stale")
        (repo / "app.py").write_text("def main():\n    return 99\n", "utf-8")
        _git(repo, "commit", "-q", "-am", "work on stale")

        worktree = Path(create_worktree(str(repo)))

        assert (worktree / "app.py").read_text("utf-8") == "def main():\n    return 1\n"

    def test_prefers_the_remote_default_branch_over_a_local_one(self, cloned: Path):
        (cloned / "app.py").write_text("def main():\n    return 2\n", "utf-8")
        _git(cloned, "commit", "-q", "-am", "local main is ahead of origin")

        assert base_ref(str(cloned)) == "origin/main"
        worktree = Path(create_worktree(str(cloned)))
        assert (worktree / "app.py").read_text("utf-8") == "def main():\n    return 1\n"

    def test_refuses_outside_a_git_repo(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GitError):
            create_worktree(str(plain))


class TestOwningRepo:
    def test_maps_a_cagents_worktree_back_to_its_checkout(self, repo: Path):
        worktree = create_worktree(str(repo))
        assert owning_repo(worktree) == str(repo)

    def test_is_empty_for_the_checkout_itself_and_for_other_worktrees(self, repo: Path):
        assert owning_repo(str(repo)) == ""
        mine = repo.parent / "hand-rolled"
        _git(repo, "worktree", "add", "-q", "-b", "mine", str(mine))
        assert owning_repo(str(mine)) == ""

    def test_is_empty_when_the_repo_is_gone(self, repo: Path, tmp_path: Path):
        orphan = tmp_path / "vanished-worktrees" / "cagents-1"
        orphan.mkdir(parents=True)
        assert owning_repo(str(orphan)) == ""


class TestListing:
    def test_reports_dirt_commits_and_merge_state(self, repo: Path):
        clean = Path(create_worktree(str(repo)))
        ahead = Path(create_worktree(str(repo)))
        dirty = Path(create_worktree(str(repo)))

        (ahead / "app.py").write_text("def main():\n    return 2\n", "utf-8")
        _git(ahead, "commit", "-q", "-am", "one commit")
        (dirty / "app.py").write_text("def main():\n    return 3\n", "utf-8")
        (dirty / "scratch.txt").write_text("notes\n", "utf-8")

        found = {w.path: w for w in cagents_worktrees(str(repo))}
        assert set(found) == {str(clean), str(ahead), str(dirty)}

        assert found[str(clean)].branch == "cagents/1"
        assert found[str(clean)].dirty_files == 0
        assert found[str(clean)].commits_ahead == 0
        assert found[str(clean)].merged is True

        assert found[str(ahead)].commits_ahead == 1
        assert found[str(ahead)].merged is False
        assert found[str(ahead)].dirty_files == 0

        assert found[str(dirty)].dirty_files == 2

    def test_excludes_worktrees_cagents_did_not_create(self, repo: Path):
        create_worktree(str(repo))
        mine = repo.parent / "hand-rolled"
        _git(repo, "worktree", "add", "-q", "-b", "mine", str(mine))

        paths = [w.path for w in cagents_worktrees(str(repo))]
        assert str(mine) not in paths
        assert len(paths) == 1


class TestRemove:
    def test_removes_a_clean_worktree_and_its_merged_branch(self, repo: Path):
        worktree = create_worktree(str(repo))

        remove_worktree(str(repo), worktree, "cagents/1")

        assert not Path(worktree).exists()
        assert cagents_worktrees(str(repo)) == []
        branches = _git(repo, "branch", "--list", "cagents/1")
        assert branches.strip() == ""
        # the last one out takes the holder directory with it
        assert not (repo.parent / "repo-worktrees").exists()

    def test_keeps_an_unmerged_branch_after_removing_its_worktree(self, repo: Path):
        worktree = Path(create_worktree(str(repo)))
        (worktree / "app.py").write_text("def main():\n    return 2\n", "utf-8")
        _git(worktree, "commit", "-q", "-am", "work worth keeping")

        remove_worktree(str(repo), str(worktree), "cagents/1")

        assert not worktree.exists()
        assert "cagents/1" in _git(repo, "branch", "--list", "cagents/1")

    def test_keeps_the_holder_while_another_worktree_lives_there(self, repo: Path):
        first = create_worktree(str(repo))
        create_worktree(str(repo))

        remove_worktree(str(repo), first, "cagents/1")

        assert (repo.parent / "repo-worktrees").is_dir()

    def test_refuses_a_dirty_worktree_loudly(self, repo: Path):
        worktree = Path(create_worktree(str(repo)))
        (worktree / "app.py").write_text("def main():\n    return 2\n", "utf-8")

        with pytest.raises(GitError):
            remove_worktree(str(repo), str(worktree), "cagents/1")
        assert worktree.exists()


# ------------------------------------------------------------ the setting ---


def test_setting_defaults_off_and_round_trips(tmp_path: Path):
    assert SETTINGS_DEFAULTS["auto_worktree"] is False
    store = Store.load(tmp_path / "state.json")
    assert store.get_setting("auto_worktree") is False
    store.set_setting("auto_worktree", True)
    assert Store.load(store.path).get_setting("auto_worktree") is True


# ------------------------------------------------------------- spawn gate ---


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").ai_title("Existing").user("go").assistant_text(
        "Done."
    ).write(claude_dir, mtime=now - 900)
    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir)
    return app, store, tmux


async def _spawn(app, pilot, payload: dict) -> None:
    app._spawn_request_path().write_text(json.dumps(payload), "utf-8")
    app.apply_snapshot(app.registry.refresh())
    await pilot.pause(0.3)
    await app.workers.wait_for_complete()
    await pilot.pause(0.2)


class TestSpawnGate:
    async def test_off_by_default_keeps_the_typed_directory(self, world, repo: Path):
        app, store, tmux = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(repo), "args": []})

            directory, _args, sid = tmux.created[-1]
            assert directory == str(repo)
            assert store.sessions[sid].project_dir == str(repo)
            assert not (repo.parent / "repo-worktrees").exists()

    async def test_on_spawns_the_conversation_in_a_fresh_worktree(self, world, repo: Path):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(repo), "args": []})

            directory, args, sid = tmux.created[-1]
            assert directory == str(repo.parent / "repo-worktrees" / "cagents-1")
            assert worktree_status(directory)[0] == "linked"
            assert current_branch(directory) == "cagents/1"
            # the conversation is bookkept where it actually runs
            assert store.sessions[sid].project_dir == directory
            assert "--session-id" in args

    async def test_each_new_conversation_gets_its_own(self, world, repo: Path):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(repo), "args": []})
            await _spawn(app, pilot, {"dir": str(repo), "args": []})

            directories = [entry[0] for entry in tmux.created]
            assert directories[-2] != directories[-1]
            assert {Path(d).name for d in directories[-2:]} == {"cagents-1", "cagents-2"}

    async def test_a_pending_n_conversation_is_bookkept_in_its_worktree(
        self, world, repo: Path
    ):
        # `n` tracks the id against the shell's directory before `claude` is
        # ever typed. Found live: without moving it, project_dir stays the
        # shared checkout, so grouping and the diff and terminal tabs all
        # point there until the first transcript record lands.
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        pending_id = "99999999-9999-9999-9999-999999999999"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._pending_new_terminals.add(pending_id)
            store.track(pending_id, str(repo), "2026-08-18T09:00:00+00:00")
            await _spawn(app, pilot, {
                "dir": str(repo), "pending_id": pending_id, "args": [],
            })

            worktree = str(repo.parent / "repo-worktrees" / "cagents-1")
            assert tmux.created[-1][0] == worktree
            assert tmux.created[-1][2] == pending_id
            assert store.sessions[pending_id].project_dir == worktree
            assert Store.load(store.path).sessions[pending_id].project_dir == worktree

    @pytest.mark.parametrize("args", [["--resume", SID2], ["--continue"], ["-c"]])
    async def test_resuming_a_conversation_never_relocates_it(self, world, repo: Path, args):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(repo), "args": args})

            assert tmux.created[-1][0] == str(repo)
            assert not (repo.parent / "repo-worktrees").exists()

    async def test_an_existing_worktree_is_used_as_is(self, world, repo: Path):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        existing = repo.parent / "hand-rolled"
        _git(repo, "worktree", "add", "-q", "-b", "mine", str(existing))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(existing), "args": []})

            assert tmux.created[-1][0] == str(existing)
            assert current_branch(tmux.created[-1][0]) == "mine"

    async def test_a_directory_outside_git_still_starts_a_conversation(self, world, tmp_path: Path):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        plain = tmp_path / "notarepo"
        plain.mkdir()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(plain), "args": []})

            assert tmux.created[-1][0] == str(plain)

    async def test_codex_conversations_get_one_too(self, world, repo: Path, monkeypatch):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        seen: list[str] = []

        def fake_codex_worker(directory, args, pending_id="", parent_id=""):
            seen.append(directory)

        monkeypatch.setattr(app, "_start_codex_worker", fake_codex_worker)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(repo), "provider": "codex", "args": []})

            assert seen == [str(repo.parent / "repo-worktrees" / "cagents-1")]

    @pytest.mark.parametrize("args", [["resume", SID2], ["fork", SID2]])
    async def test_codex_resume_and_fork_never_relocate(self, world, repo: Path, monkeypatch, args):
        app, store, tmux = world
        store.set_setting("auto_worktree", True)
        seen: list[str] = []
        monkeypatch.setattr(
            app, "_start_codex_worker",
            lambda directory, a, pending_id="", parent_id="": seen.append(directory),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _spawn(app, pilot, {"dir": str(repo), "provider": "codex", "args": args})

            assert seen == [str(repo)]


# -------------------------------------------------- mapping back to a repo ---


class TestWorktreesReadAsOneProject:
    async def test_grouped_view_groups_them_under_the_checkout(
        self, claude_dir: Path, tmp_path: Path, now: float, repo: Path
    ):
        one = create_worktree(str(repo))
        two = create_worktree(str(repo))
        TB(SID1, one).ai_title("First").user("go").assistant_text("ok").write(
            claude_dir, mtime=now - 900
        )
        TB(SID2, two).ai_title("Second").user("go").assistant_text("ok").write(
            claude_dir, mtime=now - 800
        )
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, one, "2026-08-18T09:00:00+00:00")
        store.track(SID2, two, "2026-08-18T09:01:00+00:00")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        app = CagentsApp(store=store, registry=registry, tmux=FakeTmux(), claude_dir=claude_dir)

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            views = {v.session_id: v for v in app.snapshot.views}
            assert views[SID1].group_dir == str(repo)
            assert views[SID2].group_dir == str(repo)
            # each still works where it actually runs
            assert views[SID1].work_dir == one
            assert views[SID2].work_dir == two

            await pilot.press("2")
            await pilot.pause()
            rendered = "\n".join(
                str(app.query_one("#grouped-list").get_option_at_index(i).prompt)
                for i in range(app.query_one("#grouped-list").option_count)
            )
            assert rendered.count(str(repo) + ")") == 1
            assert "cagents-1)" not in rendered

    def test_directory_shortcuts_offer_the_checkout_not_the_worktrees(
        self, claude_dir: Path, tmp_path: Path, repo: Path
    ):
        one = create_worktree(str(repo))
        two = create_worktree(str(repo))
        store = Store.load(tmp_path / "state.json")
        store.track(SID1, one, "2026-08-18T09:00:00+00:00")
        store.track(SID2, two, "2026-08-18T09:01:00+00:00")
        registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=claude_dir)
        app = CagentsApp(store=store, registry=registry, tmux=FakeTmux(), claude_dir=claude_dir)

        assert app._recent_directories() == [str(repo)]
