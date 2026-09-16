"""The git layer behind a worktree per conversation, against real temporary
repositories: naming and slot reuse, the ref a new branch is cut from,
mapping a worktree back to the checkout it came from, and what removing one
is allowed to cost."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
