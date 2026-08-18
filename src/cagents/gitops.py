"""Git operations cagents needs: worktree diffs for review, and PR
lookup/status via `gh`.

Everything here is deliberately shallow — plain `git`/`gh` subprocesses,
loud failures (GitError with the tool's own stderr), no state, and strictly
read-only: cagents never mutates a repository.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: str, timeout: float = 30.0, input_text: str | None = None) -> str:
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input_text
        )
    except FileNotFoundError:
        raise GitError(f"{args[0]} is not installed")
    except subprocess.TimeoutExpired:
        raise GitError(f"{' '.join(args[:3])}… timed out")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise GitError(f"{' '.join(args[:3])}… failed: {detail[:300]}")
    return proc.stdout


def is_git_repo(directory: str) -> bool:
    try:
        return _run(["git", "rev-parse", "--is-inside-work-tree"], directory).strip() == "true"
    except GitError:
        return False


def current_branch(directory: str) -> str:
    try:
        return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], directory).strip()
    except GitError:
        return ""


def default_branch(directory: str) -> str:
    """The repo's main line: origin/HEAD if known, else main/master, else HEAD."""
    try:
        ref = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], directory).strip()
        return ref.rsplit("/", 1)[-1]
    except GitError:
        pass
    for name in ("main", "master"):
        try:
            _run(["git", "rev-parse", "--verify", "--quiet", name], directory)
            return name
        except GitError:
            continue
    return ""


# ---------------------------------------------------------------- diffs --


@dataclass
class DiffLine:
    kind: str  # "file" | "hunk" | "add" | "del" | "ctx" | "meta"
    text: str
    file: str = ""
    new_lineno: int = 0  # 0 = not a line in the new file
    old_lineno: int = 0


@dataclass
class WorktreeDiff:
    directory: str
    branch: str
    base: str  # what the diff is against (branch name or "uncommitted")
    lines: list[DiffLine] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0

    @property
    def empty(self) -> bool:
        return not self.lines


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified_diff(diff_text: str) -> list[DiffLine]:
    """Parse `git diff` output into typed, line-numbered rows."""
    lines: list[DiffLine] = []
    current_file = ""
    old_no = new_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            # take the b/ path
            parts = raw.split(" b/", 1)
            current_file = parts[1] if len(parts) == 2 else raw[11:]
            lines.append(DiffLine("file", current_file, file=current_file))
            continue
        if raw.startswith(("index ", "--- ", "+++ ", "new file mode", "deleted file mode",
                           "similarity index", "rename from", "rename to", "old mode", "new mode",
                           "Binary files")):
            if raw.startswith("Binary files"):
                lines.append(DiffLine("meta", raw, file=current_file))
            continue
        match = _HUNK_RE.match(raw)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(2))
            lines.append(DiffLine("hunk", raw, file=current_file))
            continue
        if not current_file:
            continue
        if raw.startswith("+"):
            lines.append(DiffLine("add", raw[1:], file=current_file, new_lineno=new_no))
            new_no += 1
        elif raw.startswith("-"):
            lines.append(DiffLine("del", raw[1:], file=current_file, old_lineno=old_no))
            old_no += 1
        elif raw.startswith("\\"):  # "\ No newline at end of file"
            lines.append(DiffLine("meta", raw, file=current_file))
        else:
            text = raw[1:] if raw.startswith(" ") else raw
            lines.append(
                DiffLine("ctx", text, file=current_file, new_lineno=new_no, old_lineno=old_no)
            )
            new_no += 1
            old_no += 1
    return lines


def worktree_diff(directory: str, context: int = 3, max_bytes: int = 2_000_000) -> WorktreeDiff:
    """Everything that changed in this worktree.

    On a feature branch: diff from the merge-base with the default branch to
    the current working tree (committed + uncommitted in one view). On the
    default branch itself: just uncommitted changes. Untracked files are
    appended as pseudo-diffs so nothing is invisible.
    """
    if not is_git_repo(directory):
        raise GitError(f"not a git repository: {directory}")
    branch = current_branch(directory)
    base_branch = default_branch(directory)

    base_ref = ""
    base_desc = "uncommitted"
    if base_branch and branch and branch != base_branch:
        try:
            base_ref = _run(["git", "merge-base", base_branch, "HEAD"], directory).strip()
            base_desc = base_branch
        except GitError:
            base_ref = ""

    args = ["git", "diff", f"--unified={context}", "--no-color"]
    if base_ref:
        args.append(base_ref)
    diff_text = _run(args, directory, timeout=60.0)

    # Untracked files, shown as additions.
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], directory
    ).splitlines()
    for path in untracked:
        full = Path(directory) / path
        try:
            if full.stat().st_size > 200_000:
                diff_text += f"diff --git a/{path} b/{path}\n"
                continue
            content = full.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        body = "".join(f"+{line}\n" for line in content.splitlines())
        count = len(content.splitlines())
        diff_text += (
            f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"@@ -0,0 +1,{count} @@\n{body}"
        )

    if len(diff_text) > max_bytes:
        diff_text = diff_text[:max_bytes]

    lines = parse_unified_diff(diff_text)
    result = WorktreeDiff(directory=directory, branch=branch, base=base_desc, lines=lines)
    for line in lines:
        if line.kind == "file":
            result.files.append(line.file)
        elif line.kind == "add":
            result.additions += 1
        elif line.kind == "del":
            result.deletions += 1
    return result


# ---------------------------------------------------- GitHub PR comments --


@dataclass
class ReviewComment:
    file: str  # "" for PR-level comments
    line: int  # 0 when not tied to a line
    body: str
    author: str
    source: str = "github"


def github_pr_comments(directory: str, gh_bin: str = "gh") -> list[ReviewComment]:
    """Inline review comments + top-level reviews for the PR of the current
    branch. Requires `gh` authenticated; fails loudly otherwise."""
    if shutil.which(gh_bin) is None:
        raise GitError("gh (GitHub CLI) is not installed")
    number = _run([gh_bin, "pr", "view", "--json", "number", "-q", ".number"], directory).strip()
    if not number:
        raise GitError("no PR found for this branch")
    comments: list[ReviewComment] = []

    inline_raw = _run(
        [gh_bin, "api", f"repos/{{owner}}/{{repo}}/pulls/{number}/comments"],
        directory,
        timeout=30.0,
    )
    for item in json.loads(inline_raw):
        if not isinstance(item, dict):
            continue
        body = str(item.get("body", "")).strip()
        if not body:
            continue
        comments.append(
            ReviewComment(
                file=str(item.get("path", "") or ""),
                line=int(item.get("line") or item.get("original_line") or 0),
                body=body,
                author=str((item.get("user") or {}).get("login", "github")),
            )
        )

    reviews_raw = _run(
        [gh_bin, "api", f"repos/{{owner}}/{{repo}}/pulls/{number}/reviews"],
        directory,
        timeout=30.0,
    )
    for item in json.loads(reviews_raw):
        if not isinstance(item, dict):
            continue
        body = str(item.get("body", "")).strip()
        if not body:
            continue
        state = str(item.get("state", ""))
        author = str((item.get("user") or {}).get("login", "github"))
        comments.append(
            ReviewComment(file="", line=0, body=f"[{state}] {body}", author=author)
        )
    return comments


# ------------------------------------------------------ PR lookup / status --


@dataclass
class PRStatus:
    merged: bool = False
    state: str = ""  # OPEN / CLOSED / MERGED
    last_activity: str = ""  # ISO timestamp of newest comment/review, "" if none


def _gh_runner_default(args: list[str], cwd: str | None = None) -> str:
    return _run(args, cwd or ".", timeout=30.0)


def find_pr_url(directory: str, runner=None) -> str:
    """The PR for this worktree's current branch, per gh; '' if none."""
    run = runner or _gh_runner_default
    try:
        out = run(["gh", "pr", "view", "--json", "url", "-q", ".url"], directory)
    except Exception:
        return ""
    url = out.strip()
    return url if url.startswith("http") else ""


def pr_status(pr_url: str, runner=None) -> PRStatus:
    """Merged? And when did a human last touch it (comment/review)?"""
    run = runner or _gh_runner_default
    out = run(
        ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,comments,reviews"], None
    )
    data = json.loads(out)
    stamps: list[str] = []
    for item in data.get("comments") or []:
        created = item.get("createdAt")
        if isinstance(created, str):
            stamps.append(created)
    for item in data.get("reviews") or []:
        submitted = item.get("submittedAt")
        if isinstance(submitted, str):
            stamps.append(submitted)
    return PRStatus(
        merged=bool(data.get("mergedAt")) or data.get("state") == "MERGED",
        state=str(data.get("state", "")),
        last_activity=max(stamps) if stamps else "",
    )
