"""Tests for git operations against real temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cagents.gitops import (
    GitError,
    PRStatus,
    current_branch,
    default_branch,
    find_pr_url,
    github_pr_comments,
    is_git_repo,
    parse_unified_diff,
    pr_status,
    worktree_diff,
    worktree_status,
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


class TestWorktreeDiff:
    def test_feature_branch_diff_committed_and_uncommitted(self, repo: Path):
        # a real git worktree, the way Claude Code's own worktrees look
        wt = repo.parent / "wt-feature"
        _git(repo, "worktree", "add", "-b", "feature-work", str(wt))
        # committed change
        (wt / "app.py").write_text("def main():\n    return 2\n", "utf-8")
        _git(wt, "commit", "-am", "change return")
        # uncommitted change
        (wt / "README.md").write_text("# repo\nmore docs\n", "utf-8")
        # untracked file
        (wt / "notes.txt").write_text("remember this\n", "utf-8")

        diff = worktree_diff(str(wt))
        assert diff.branch == "feature-work"
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


class TestWorktreeStatus:
    def test_linked_worktree_is_told_apart_from_the_main_checkout(self, repo: Path):
        wt = repo.parent / "wt-feature"
        _git(repo, "worktree", "add", "-b", "feature-work", str(wt))
        kind, root = worktree_status(str(wt))
        assert kind == "linked"
        assert Path(root).resolve() == wt.resolve()

    def test_main_checkout_is_not_a_linked_worktree(self, repo: Path):
        kind, root = worktree_status(str(repo))
        assert kind == "main"
        assert Path(root).resolve() == repo.resolve()

    def test_non_repo_has_no_worktree_status(self, tmp_path: Path):
        plain = tmp_path / "nope"
        plain.mkdir()
        assert worktree_status(str(plain)) == ("", "")

    def test_missing_directory_has_no_worktree_status(self, tmp_path: Path):
        assert worktree_status(str(tmp_path / "does-not-exist")) == ("", "")


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


class TestPRStatus:
    def test_find_pr_url(self):
        calls = []

        def runner(args, cwd=None):
            calls.append(args)
            return "https://github.com/o/r/pull/7\n"

        assert find_pr_url("/proj", runner=runner) == "https://github.com/o/r/pull/7"
        assert calls[0][:3] == ["gh", "pr", "view"]

    def test_find_pr_url_none(self):
        def runner(args, cwd=None):
            raise RuntimeError("no pull requests found")

        assert find_pr_url("/proj", runner=runner) == ""

    def test_pr_status_merged_and_activity(self):
        import json as _json

        payload = _json.dumps({
            "state": "MERGED", "mergedAt": "2026-08-18T10:00:00Z",
            "comments": [{"createdAt": "2026-08-18T09:00:00Z"}],
            "reviews": [{"submittedAt": "2026-08-18T09:30:00Z"}],
        })
        status = pr_status("url", runner=lambda a, cwd=None: payload)
        assert status.merged is True
        assert status.last_activity == "2026-08-18T09:30:00Z"

    def test_pr_status_open_no_activity(self):
        import json as _json

        payload = _json.dumps({"state": "OPEN", "mergedAt": None,
                               "comments": [], "reviews": []})
        status = pr_status("url", runner=lambda a, cwd=None: payload)
        assert status.merged is False and status.last_activity == ""
