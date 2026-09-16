"""Git operations cagents needs: worktree diffs for review, PR
lookup/status via `gh`, and the per-conversation worktrees the
auto_worktree setting grows.

Everything here is deliberately shallow — plain `git`/`gh` subprocesses,
loud failures (GitError with the tool's own stderr), no state.

Reads stay reads. The only writes are worktree add/remove under the
`cagents/` branch prefix, always on a branch of cagents' own making:
nothing here touches a branch, a commit, or a file a human wrote.
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


def worktree_status(directory: str) -> tuple[str, str]:
    """Where `directory` sits relative to git, distinguishing a genuine
    *linked* worktree (`git worktree add`) from the repo's main checkout —
    both are "a git working tree" as far as `is_git_repo` is concerned,
    but only the former is a dedicated space for one session to work in
    without colliding with anyone else using the same repo.

    A linked worktree's private git-dir lives under the main repo's
    `.git/worktrees/<name>`, so it differs from the shared "common" git
    dir; the main checkout's git-dir and common-dir are the same path.
    That comparison is the standard, reliable way to tell them apart.

    Returns ("linked", worktree_root), ("main", repo_root), or ("", "")
    if `directory` isn't inside a git working tree at all."""
    try:
        git_dir = _run(["git", "rev-parse", "--git-dir"], directory).strip()
        common_dir = _run(["git", "rev-parse", "--git-common-dir"], directory).strip()
        toplevel = _run(["git", "rev-parse", "--show-toplevel"], directory).strip()
    except GitError:
        return "", ""
    # git prints paths relative to the queried directory, not to this
    # process's own cwd — resolve against `directory`, not bare Path().
    git_dir_abs = Path(directory, git_dir).resolve()
    common_dir_abs = Path(directory, common_dir).resolve()
    kind = "linked" if git_dir_abs != common_dir_abs else "main"
    return kind, toplevel


def default_branch(directory: str) -> str:
    """The repo's main line — the thing a worktree diff compares against.

    Remote-tracking refs are preferred over local branches: a local `main`
    can be stale or missing entirely in a linked worktree, while
    origin/main is what "versus master" actually means."""
    try:
        ref = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], directory).strip()
        return ref.rsplit("/", 1)[-1]
    except GitError:
        pass
    for name in ("origin/main", "origin/master", "main", "master"):
        try:
            _run(["git", "rev-parse", "--verify", "--quiet", name], directory)
            return name
        except GitError:
            continue
    return ""


# ---------------------------------------------------- session worktrees --

# Both halves of a cagents worktree's identity: the directory
# `<repo>-worktrees/cagents-<n>` and the branch `cagents/<n>`. The prefix is
# what marks a worktree as ours — nothing else is ever touched.
WORKTREE_PREFIX = "cagents"
_HOLDER_SUFFIX = "-worktrees"
_MAX_SLOTS = 500


def base_ref(directory: str) -> str:
    """A concrete, verified ref to branch a new worktree off: this repo's
    mainline, remote-tracking first.

    `default_branch` answers a related question for the diff view, but it
    returns a bare *name* — its origin/HEAD lookup keeps only the last
    path component, so "origin/main" comes back as "main". For a diff base
    that is harmless; for `git worktree add` it is the difference between
    starting from the mainline and starting from whatever a local `main`
    was at the last pull. Every candidate here is verified, so the answer
    is a ref that exists."""
    try:
        head = _run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], directory
        ).strip()
    except GitError:
        head = ""
    for name in (head, "origin/main", "origin/master", "main", "master"):
        if not name:
            continue
        try:
            _run(["git", "rev-parse", "--verify", "--quiet", name], directory)
            return name
        except GitError:
            continue
    return ""


def worktree_holder(repo_dir: str) -> Path:
    """The sibling directory every worktree of `repo_dir` lives under."""
    repo = Path(repo_dir).resolve()
    return repo.parent / f"{repo.name}{_HOLDER_SUFFIX}"


def owning_repo(directory: str) -> str:
    """The checkout a cagents-created worktree belongs to; '' for anything
    else, including worktrees someone made by hand.

    Read off the layout cagents itself lays down rather than out of git:
    this runs for every conversation on every refresh, and two
    subprocesses per row is not a price a list render can pay."""
    path = Path(directory)
    holder = path.parent
    if not path.name.startswith(f"{WORKTREE_PREFIX}-"):
        return ""
    if not holder.name.endswith(_HOLDER_SUFFIX):
        return ""
    repo = holder.parent / holder.name[: -len(_HOLDER_SUFFIX)]
    return str(repo) if repo.is_dir() else ""


def next_worktree_slot(repo_dir: str) -> tuple[str, str]:
    """(destination, branch) for this repo's next worktree — the lowest
    number whose directory *and* branch are both free, so removing an old
    one hands its number back."""
    holder = worktree_holder(repo_dir)
    for number in range(1, _MAX_SLOTS):
        dest = holder / f"{WORKTREE_PREFIX}-{number}"
        branch = f"{WORKTREE_PREFIX}/{number}"
        if dest.exists():
            continue
        try:
            _run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo_dir)
        except GitError:
            return str(dest), branch
    raise GitError(f"no free worktree slot under {holder}")


def create_worktree(repo_dir: str) -> str:
    """Grow `repo_dir` a dedicated worktree for one conversation, branched
    off the mainline. Returns its path; raises GitError carrying git's own
    message if anything goes wrong."""
    if not is_git_repo(repo_dir):
        raise GitError(f"not a git repository: {repo_dir}")
    dest, branch = next_worktree_slot(repo_dir)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "worktree", "add", "-b", branch, dest]
    base = base_ref(repo_dir)
    if base:
        args.append(base)
    _run(args, repo_dir, timeout=120.0)
    return dest


@dataclass
class CagentsWorktree:
    path: str
    branch: str
    dirty_files: int = 0
    commits_ahead: int = 0  # commits on the branch that the mainline lacks
    merged: bool = False  # the mainline already contains the branch tip


def cagents_worktrees(repo_dir: str) -> list[CagentsWorktree]:
    """Every worktree of this repo that cagents created, each with enough
    state to decide whether removing it would lose anything."""
    try:
        listing = _run(["git", "worktree", "list", "--porcelain"], repo_dir)
    except GitError:
        return []
    found: list[CagentsWorktree] = []
    path = ""
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
            if path and branch.startswith(f"{WORKTREE_PREFIX}/"):
                found.append(CagentsWorktree(path=path, branch=branch))
            path = ""
    base = base_ref(repo_dir)
    for entry in found:
        entry.dirty_files = _dirty_count(entry.path)
        if not base:
            continue
        entry.commits_ahead = _commit_count(
            ["git", "rev-list", "--count", f"{base}..{entry.branch}"], repo_dir
        )
        entry.merged = _is_ancestor(entry.branch, base, repo_dir)
    return found


def remove_worktree(repo_dir: str, worktree_path: str, branch: str = "") -> None:
    """Remove a worktree cagents created.

    git itself refuses while the worktree holds modified or untracked
    files, and that refusal is passed straight through. The branch goes
    only if git agrees it is merged (`branch -d`, never `-D`), so work
    that was committed but not landed outlives its worktree."""
    _run(["git", "worktree", "remove", worktree_path], repo_dir, timeout=60.0)
    holder = Path(worktree_path).parent
    if holder.name.endswith(_HOLDER_SUFFIX):
        try:
            holder.rmdir()  # only when the last worktree of this repo went
        except OSError:
            pass
    if not branch:
        return
    try:
        _run(["git", "branch", "-d", branch], repo_dir)
    except GitError:
        pass  # unmerged: keeping the branch is the whole point


def _dirty_count(directory: str) -> int:
    try:
        out = _run(["git", "status", "--porcelain"], directory)
    except GitError:
        return 0
    return len([line for line in out.splitlines() if line.strip()])


def _commit_count(args: list[str], cwd: str) -> int:
    try:
        return int(_run(args, cwd).strip() or 0)
    except (GitError, ValueError):
        return 0


def _is_ancestor(branch: str, base: str, cwd: str) -> bool:
    try:
        _run(["git", "merge-base", "--is-ancestor", branch, base], cwd)
        return True
    except GitError:
        return False


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
    closed: bool = False  # closed WITHOUT merging
    state: str = ""  # OPEN / CLOSED / MERGED
    last_activity: str = ""  # ISO timestamp of newest comment/review, "" if none
    updated_at: str = ""  # ISO timestamp of ANY change to the PR
    # Per-trigger-category timestamps, each "" if that category never
    # happened — lets the external-update settings gate on exactly the
    # kind of activity that occurred, not just "something changed".
    last_comment_from_others: str = ""
    last_comment_from_self: str = ""  # own_login's own comments
    last_review: str = ""
    last_commit: str = ""


def _gh_runner_default(args: list[str], cwd: str | None = None) -> str:
    return _run(args, cwd or ".", timeout=30.0)


def current_github_login(runner=None) -> str:
    """The authenticated gh user's own login — lets pr_status tell a
    comment someone else left apart from one you left yourself, so
    self-comments can be gated by their own (default-off) setting
    without hiding real feedback from others."""
    run = runner or _gh_runner_default
    try:
        out = run(["gh", "api", "user", "--jq", ".login"], None)
    except Exception:
        return ""
    return out.strip()


def find_pr_url(directory: str, runner=None, expected_branch: str = "") -> str:
    """The PR for whatever branch is CURRENTLY checked out in `directory`,
    per gh; '' if none, or if the result can't be trusted.

    Real bug, confirmed live (repeatedly — a whole batch of unrelated
    sessions all "inheriting" the same Jira card): `directory` is very
    often a SHARED checkout, not a dedicated worktree — several cagents
    sessions (e.g. a handoff-created session, which inherits its parent's
    plain project dir) can point at the exact same directory. `gh pr
    view` there answers "what's checked out right now", not "this
    session's own PR" — so without a check, one session's lookup can
    silently attach a completely unrelated session's PR (and from there,
    its Jira card) the moment someone else's branch happens to be checked
    out at call time — and because that gets cached into the session's
    own tracked.pr_url, it then sticks forever, never re-checked again.

    The worktree check below is UNCONDITIONAL, not just "when a branch
    was given to check against" — that was the actual hole: a session
    with no recorded `gitBranch` yet (a near-empty transcript; there's no
    shortage of those) skipped the whole guard and fell straight back to
    trusting a shared checkout's current branch, for every such session
    pointed at that directory, all inheriting whichever PR happened to be
    checked out there at poll time.

      1. `directory` must be a genuinely dedicated (linked) worktree, not
         the shared main checkout — ALWAYS required, branch known or not:
         a branch match in a shared checkout is no guarantee either,
         since the "checked out branch" there is really just whatever
         some OTHER session or a human last left it as.
      2. If `expected_branch` (the session's own last-known branch, from
         its own transcript's `gitBranch` field — never from re-inspecting
         the directory) is given, the found PR's actual branch must equal
         it too.
    Either failing returns '' (fail closed; mapping the WRONG PR is worse
    than finding none) rather than a plausible-looking wrong answer."""
    kind, _root = worktree_status(directory)
    if kind != "linked":
        return ""
    run = runner or _gh_runner_default
    try:
        out = run(["gh", "pr", "view", "--json", "url,headRefName"], directory)
    except Exception:
        return ""
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return ""
    url = str(data.get("url", "") or "")
    if not url.startswith("http"):
        return ""
    if expected_branch and str(data.get("headRefName", "") or "") != expected_branch:
        return ""
    return url


def prs_referencing(text: str, directory: str, runner=None, limit: int = 10) -> list[str]:
    """PR urls in `directory`'s repo that mention `text` — how a Jira key
    finds the PRs written for it, the reverse of pr_jira_sources.

    Closed and merged PRs count: the card's PR is just as often already
    landed. `text` is passed as its own argv element, never a shell
    string. Any failure is '[]' — a search that finds nothing and a gh
    that cannot run are the same answer to the caller."""
    if not text.strip():
        return []
    run = runner or _gh_runner_default
    try:
        out = run(
            [
                "gh", "pr", "list",
                "--search", text,
                "--state", "all",
                "--limit", str(limit),
                "--json", "url",
            ],
            directory,
        )
    except Exception:
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [
        str(item["url"])
        for item in data
        if isinstance(item, dict) and str(item.get("url", "")).startswith("http")
    ]


def pr_jira_sources(pr_url: str, runner=None) -> tuple[str, str, str]:
    """(title, body, branch) of a specific PR, by URL — the text a Jira key
    search checks in order. Takes the URL directly (like pr_status), not a
    directory, so it can't pick up whatever branch happens to be checked
    out in a shared worktree right now."""
    run = runner or _gh_runner_default
    try:
        out = run(["gh", "pr", "view", pr_url, "--json", "title,body,headRefName"], None)
    except Exception:
        return "", "", ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return "", "", ""
    return (
        str(data.get("title", "") or ""),
        str(data.get("body", "") or ""),
        str(data.get("headRefName", "") or ""),
    )


def pr_status(pr_url: str, runner=None, own_login: str = "") -> PRStatus:
    """Merged? And when did a human last touch it (comment/review/push)?

    `own_login` (current_github_login()) splits comments into "from
    others" vs "from yourself" — the latter defaults off in settings,
    since your own comment on your own PR isn't something you generally
    need re-alerting about."""
    run = runner or _gh_runner_default
    out = run(
        ["gh", "pr", "view", pr_url, "--json",
         "state,mergedAt,comments,reviews,commits,updatedAt"], None
    )
    data = json.loads(out)
    others: list[str] = []
    mine: list[str] = []
    reviews: list[str] = []
    commits: list[str] = []
    for item in data.get("comments") or []:
        created = item.get("createdAt")
        if not isinstance(created, str):
            continue
        author = str((item.get("author") or {}).get("login", ""))
        (mine if own_login and author == own_login else others).append(created)
    for item in data.get("reviews") or []:
        submitted = item.get("submittedAt")
        if isinstance(submitted, str):
            reviews.append(submitted)
    for item in data.get("commits") or []:
        committed = item.get("committedDate")
        if isinstance(committed, str):
            commits.append(committed)
    stamps = others + mine + reviews
    merged = bool(data.get("mergedAt")) or data.get("state") == "MERGED"
    return PRStatus(
        merged=merged,
        closed=(data.get("state") == "CLOSED") and not merged,
        state=str(data.get("state", "")),
        last_activity=max(stamps) if stamps else "",
        updated_at=str(data.get("updatedAt") or ""),
        last_comment_from_others=max(others) if others else "",
        last_comment_from_self=max(mine) if mine else "",
        last_review=max(reviews) if reviews else "",
        last_commit=max(commits) if commits else "",
    )
