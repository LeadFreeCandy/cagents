"""Tests for git operations against real temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cagents.gitops import (
    GitError,
    create_worktree,
    current_branch,
    default_branch,
    github_pr_comments,
    is_git_repo,
    parse_unified_diff,
    remove_worktree,
    slugify,
    worktree_diff,
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def main():\n    return 1\n", "utf-8")
    (r / "README.md").write_text("# repo\n", "utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "initial")
    return r


def test_is_git_repo(repo: Path, tmp_path: Path):
    assert is_git_repo(str(repo)) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_git_repo(str(plain)) is False


def test_slugify():
    assert slugify("Add auth: tests & fixes!") == "add-auth-tests-fixes"
    assert slugify("   ") == "work"
    assert len(slugify("x" * 100)) <= 40


def test_create_and_remove_worktree(repo: Path):
    path = create_worktree(str(repo), "Add auth tests")
    assert Path(path).is_dir()
    assert current_branch(path) == "todo/add-auth-tests"
    assert Path(path).name == "add-auth-tests"
    # creating again fails loudly
    with pytest.raises(GitError):
        create_worktree(str(repo), "Add auth tests")
    remove_worktree(str(repo), path)
    assert not Path(path).exists()


def test_remove_dirty_worktree_refuses(repo: Path):
    path = create_worktree(str(repo), "risky work")
    (Path(path) / "new.py").write_text("x = 1\n", "utf-8")
    with pytest.raises(GitError):
        remove_worktree(str(repo), path)
    assert Path(path).exists()  # untouched


class TestWorktreeDiff:
    def test_feature_branch_diff_committed_and_uncommitted(self, repo: Path):
        path = create_worktree(str(repo), "feature work")
        wt = Path(path)
        # committed change
        (wt / "app.py").write_text("def main():\n    return 2\n", "utf-8")
        _git(wt, "commit", "-am", "change return")
        # uncommitted change
        (wt / "README.md").write_text("# repo\nmore docs\n", "utf-8")
        # untracked file
        (wt / "notes.txt").write_text("remember this\n", "utf-8")

        diff = worktree_diff(str(wt))
        assert diff.branch == "todo/feature-work"
        assert diff.base == "main"
        assert set(diff.files) == {"app.py", "README.md", "notes.txt"}
        assert diff.additions >= 3  # return 2, more docs, remember this
        assert diff.deletions >= 1  # return 1
        texts = [(l.kind, l.text) for l in diff.lines]
        assert ("add", "    return 2") in texts
        assert ("del", "    return 1") in texts
        assert ("add", "remember this") in texts

    def test_default_branch_shows_uncommitted_only(self, repo: Path):
        (repo / "app.py").write_text("def main():\n    return 3\n", "utf-8")
        diff = worktree_diff(str(repo))
        assert diff.base == "uncommitted"
        assert diff.files == ["app.py"]

    def test_clean_worktree_is_empty(self, repo: Path):
        diff = worktree_diff(str(repo))
        assert diff.empty

    def test_non_repo_fails_loudly(self, tmp_path: Path):
        plain = tmp_path / "nope"
        plain.mkdir()
        with pytest.raises(GitError):
            worktree_diff(str(plain))


class TestParseUnifiedDiff:
    DIFF = """\
diff --git a/src/x.py b/src/x.py
index 111..222 100644
--- a/src/x.py
+++ b/src/x.py
@@ -10,4 +10,5 @@ def f():
 context line
-old line
+new line
+added line
 tail line
diff --git a/bin/blob b/bin/blob
Binary files a/bin/blob and b/bin/blob differ
"""

    def test_line_numbers_and_kinds(self):
        lines = parse_unified_diff(self.DIFF)
        kinds = [l.kind for l in lines]
        assert kinds == ["file", "hunk", "ctx", "del", "add", "add", "ctx", "file", "meta"]
        ctx = lines[2]
        assert (ctx.old_lineno, ctx.new_lineno) == (10, 10)
        deleted = lines[3]
        assert deleted.old_lineno == 11 and deleted.new_lineno == 0
        added = lines[4]
        assert added.new_lineno == 11
        added2 = lines[5]
        assert added2.new_lineno == 12
        tail = lines[6]
        assert (tail.old_lineno, tail.new_lineno) == (12, 13)
        assert all(l.file == "src/x.py" for l in lines[:7])

    def test_garbage_tolerated(self):
        assert parse_unified_diff("") == []
        assert parse_unified_diff("random text\nno diff here") == []


class TestGithubComments:
    def test_missing_gh_fails_loudly(self, repo: Path, monkeypatch):
        with pytest.raises(GitError) as err:
            github_pr_comments(str(repo), gh_bin="definitely-not-a-real-binary")
        assert "not installed" in str(err.value)
