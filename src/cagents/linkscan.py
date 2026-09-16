"""Which PR / Jira card does this conversation belong to?

`o` and `O` open a session's PR and Jira card. When neither is linked
yet, the conversation itself is usually the best evidence available — it
mentions the PR Claude opened, the card the work started from, and
(unhelpfully) every other PR and card that came up along the way.

So: read the transcript, collect every PR URL and Jira key it mentions,
and weigh each by *where* it was mentioned. A PR Claude recorded opening
outranks one you pasted for context, which outranks one it noticed in
passing; a key in the branch name or your opening request outranks one
from the middle of a tool result.

Likelihoods are shares of the total evidence, with a fixed weight held
back for "none of these" — so one weak mention reads as one weak
mention, not a certainty.

No network and no git: everything here comes from the transcript plus
what the caller already knows (project directory, branch).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Probability mass "none of these" always keeps.
NONE_OF_THESE_WEIGHT = 3.0

_PR_URL_RE = re.compile(r"https?://github\.com/([\w.-]+?)/([\w.-]+?)/pull/(\d+)")
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")

# These match a Jira key's shape and never name a card. Without this the
# scanner offers UTF-8 and SHA-256 as tickets.
_NOT_TICKET_PREFIXES = frozenset(
    {
        "AES", "ANSI", "API", "ARM", "ASCII", "BMP", "CIDR", "CRC", "CSS", "CVE",
        "DES", "EC2", "ECMA", "EOF", "ES", "GIF", "GPT", "HMAC", "HTML", "HTTP",
        "HTTPS", "IEEE", "IPV", "ISO", "JPEG", "JSON", "LTS", "MD", "MD5", "MP3",
        "MP4", "NFC", "NFKC", "OAUTH", "PEP", "PNG", "POSIX", "RFC", "RGB", "RGBA",
        "RSA", "SHA", "SHA1", "SHA256", "SQL", "SSL", "TLS", "URL", "UTF", "UUID",
        "X86",
    }
)


@dataclass
class Candidate:
    """One thing the conversation mentions, and why it might be the one."""

    value: str  # the PR url, or the Jira key
    label: str  # "PR #42" / "OWNER-663"
    weight: float = 0.0
    reasons: list[str] = field(default_factory=list)
    likelihood: float = 0.0  # share of the evidence, "none of these" held back


def pr_candidates(path: Path, project_dir: str = "") -> list[Candidate]:
    """PR URLs this conversation mentions, likeliest first."""
    evidence = _collect(_mark_created(_read(path)), _pr_in)
    repo = _repo_name(project_dir)
    for value, entry in evidence.items():
        if repo and _same_repo(value, repo):
            entry.extra.append((2.0, "same repo as this session"))
    return _rank(evidence, _PR_WEIGHTS)


def jira_candidates(path: Path, branch: str = "") -> list[Candidate]:
    """Jira keys this conversation mentions, likeliest first."""
    evidence = _collect(_read(path), _jira_in)
    for key, _ in _jira_in(branch):
        # The branch is the strongest signal there is, and it is often the
        # only place the key appears at all.
        entry = evidence.setdefault(key, _Evidence(label=key))
        entry.extra.append((6.0, "in the branch name"))
    return _rank(evidence, _JIRA_WEIGHTS)


# -- reading the transcript ------------------------------------------------


def _read(path: Path) -> list[tuple[str, str]]:
    """(where, text) for every piece of the transcript, in order.

    `where` is what the weighting keys off: "pr-link", "title",
    "first-prompt", "user", "assistant", "thinking", "command",
    "output", "meta". The opening request is just the first plain user
    message, promoted after the fact.
    """
    try:
        lines = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []

    pieces: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            pieces.extend(_record_pieces(record))
    for index, (where, text) in enumerate(pieces):
        if where == "user":
            pieces[index] = ("first-prompt", text)
            break
    return pieces


def _record_pieces(record: dict) -> list[tuple[str, str]]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return _codex_pieces(str(record.get("type", "")), payload)
    return _claude_pieces(record)


def _claude_pieces(record: dict) -> list[tuple[str, str]]:
    kind = str(record.get("type", ""))
    if kind == "pr-link":
        return [("pr-link", str(record.get("prUrl", "")))]
    if kind in ("ai-title", "custom-title"):
        return [("title", str(record.get("aiTitle") or record.get("customTitle") or ""))]
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    role = str(message.get("role", ""))
    content = message.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    pieces: list[tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", ""))
        if block_type == "tool_result":
            pieces.append(("output", _flatten(block.get("content"))))
        elif block_type == "tool_use":
            pieces.append(("command", _flatten(block.get("input"))))
        elif block_type == "thinking":
            pieces.append(("thinking", str(block.get("thinking", ""))))
        elif block_type == "text":
            text = str(block.get("text", ""))
            if role != "user":
                pieces.append(("assistant", text))
            else:
                pieces.append(("meta" if record.get("isMeta") else "user", text))
    return pieces


def _codex_pieces(kind: str, payload: dict) -> list[tuple[str, str]]:
    """The same signals out of a Codex rollout, which nests everything
    under `payload` instead (see codex_data.parse_session_file)."""
    from .codex_data import message_text

    payload_type = str(payload.get("type", ""))
    if kind == "event_msg":
        if payload_type in ("user_message", "agent_message"):
            where = "user" if payload_type == "user_message" else "assistant"
            return [(where, str(payload.get("message") or ""))]
        return []
    if kind != "response_item":
        return []
    if payload_type == "message" and payload.get("role") in ("user", "assistant"):
        return [(str(payload["role"]), message_text(payload))]
    if payload_type in ("function_call", "custom_tool_call"):
        return [("command", _flatten(payload.get("arguments") or payload.get("input") or ""))]
    if payload_type in ("function_call_output", "custom_tool_call_output"):
        return [("output", _flatten(payload.get("output")))]
    if payload_type == "reasoning":
        return [("thinking", _flatten(payload.get("summary") or payload.get("content")))]
    return []


_CREATES_PR_RE = re.compile(r"\bpr\s+create\b")


def _mark_created(pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Re-tag the output of a PR-creating command as "pr-created".

    A PR opened by `gh pr create` in a Bash call leaves no pr-link record
    anywhere — the URL exists only in that command's output, which is
    otherwise the weakest place a URL can turn up. It is the same event
    as a pr-link record, so it carries the same weight.
    """
    marked = list(pieces)
    creating = False
    for index, (where, text) in enumerate(marked):
        if where == "command":
            creating = bool(_CREATES_PR_RE.search(text))
        elif where == "output" and creating:
            marked[index] = ("pr-created", text)
            creating = False
    return marked


def _flatten(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


# -- finding the mentions --------------------------------------------------


def _pr_in(text: str) -> list[tuple[str, str]]:
    """(normalized url, label) per PR mentioned — one entry per mention, so
    repetition still counts."""
    found = []
    for owner, repo, number in _PR_URL_RE.findall(text):
        found.append((f"https://github.com/{owner}/{repo}/pull/{number}", f"PR #{number}"))
    return found


def _jira_in(text: str) -> list[tuple[str, str]]:
    found = []
    for prefix, number in _JIRA_KEY_RE.findall(text):
        if prefix in _NOT_TICKET_PREFIXES:
            continue
        key = f"{prefix}-{number}"
        found.append((key, key))
    return found


@dataclass
class _Evidence:
    label: str
    wheres: set[str] = field(default_factory=set)
    mentions: int = 0
    extra: list[tuple[float, str]] = field(default_factory=list)  # (weight, reason)


def _collect(pieces: list[tuple[str, str]], find) -> dict[str, _Evidence]:
    evidence: dict[str, _Evidence] = {}
    for where, text in pieces:
        if not text:
            continue
        for value, label in find(text):
            entry = evidence.setdefault(value, _Evidence(label=label))
            entry.wheres.add(where)
            entry.mentions += 1
    return evidence


# -- weighing it -----------------------------------------------------------


# How much it means that a mention turned up in a given part of the
# transcript. Each *distinct* place counts once — being in the title and
# the opening request is real corroboration; being repeated 40 times in
# one tool result is not, so repetition is capped separately below.
_PR_WEIGHTS = {
    "pr-link": (6.0, "Claude opened this PR here"),
    "pr-created": (6.0, "created by gh pr create here"),
    "first-prompt": (3.0, "in your opening request"),
    "user": (3.0, "you mentioned it"),
    "title": (2.0, "in the session title"),
    "command": (1.5, "used in a command"),
    "output": (1.5, "appeared in command output"),
    "assistant": (1.0, "Claude mentioned it"),
    "thinking": (0.5, "Claude mentioned it"),
    "meta": (0.25, "in injected context"),
}

_JIRA_WEIGHTS = {
    "first-prompt": (5.0, "in your opening request"),
    "title": (3.0, "in the session title"),
    "user": (2.5, "you mentioned it"),
    "command": (1.0, "used in a command"),
    "output": (1.0, "appeared in command output"),
    "assistant": (1.0, "Claude mentioned it"),
    "thinking": (0.5, "Claude mentioned it"),
    "meta": (0.25, "in injected context"),
}

_REPEAT_WEIGHT = 0.5
_REPEAT_CAP = 2.0


def _repo_name(project_dir: str) -> str:
    return Path(project_dir).name if project_dir else ""


def _same_repo(url: str, repo: str) -> bool:
    """Whether a PR's repo looks like this session's checkout. Worktrees
    are routinely named `<repo>-<branch>`, so a short repo name has to
    match exactly but a real one may be a prefix of the directory."""
    match = _PR_URL_RE.match(url)
    if match is None:
        return False
    name = match.group(2).lower()
    folder = repo.lower()
    return name == folder or (len(name) >= 3 and name in folder)


def _rank(evidence: dict[str, _Evidence], weights: dict[str, tuple[float, str]]) -> list[Candidate]:
    candidates = []
    for value, entry in evidence.items():
        scored = [weights[where] for where in entry.wheres if where in weights]
        scored.extend(entry.extra)
        weight = sum(w for w, _ in scored)
        scored.sort(reverse=True)  # strongest reason first, and deduped
        reasons = list(dict.fromkeys(reason for _, reason in scored))
        repeats = min((entry.mentions - 1) * _REPEAT_WEIGHT, _REPEAT_CAP)
        if repeats > 0:
            weight += repeats
            reasons.append(f"mentioned {entry.mentions} times")
        if weight <= 0:
            continue
        candidates.append(
            Candidate(value=value, label=entry.label, weight=weight, reasons=reasons)
        )
    total = sum(c.weight for c in candidates) + NONE_OF_THESE_WEIGHT
    for candidate in candidates:
        candidate.likelihood = candidate.weight / total
    candidates.sort(key=lambda c: (-c.weight, c.value))
    return candidates
