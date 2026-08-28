"""Fuzzy full-text search across every Claude conversation transcript on
disk — the complete message history, not just titles or the display
preview (which deliberately only reads a head/tail window of large files
for speed; see claude_data.HEAD_BYTES/TAIL_BYTES). A full scan can be slow
across many/large transcripts, so this is opt-in (conversation_search
setting) and only runs when the user explicitly asks.

Results are tiered by MatchKind, not sorted by one flat score: a
conversation's own name matching always outranks a body hit, and an exact
substring always outranks a fuzzy one — see MatchKind's docstring. This is
fixed ranking behavior, not a setting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from .claude_data import DiscoveredSession, _iter_content_blocks, _result_text, discover_sessions

_WORD_BOUNDARY = set(" _-./:\n\t()[]{}")


class MatchKind(IntEnum):
    """Priority tier for a search hit — lower sorts first, and always
    wins over a higher tier regardless of score. A conversation's own
    name matching outright beats anything merely mentioned somewhere in
    its body (the whole point of this ranking); within a tier, an exact
    substring always outranks a fuzzy (scattered-character) one, so a
    deliberately-typed phrase never loses to a scattered coincidence."""

    TITLE_EXACT = 0
    TITLE_FUZZY = 1
    CONTENT_EXACT = 2
    CONTENT_FUZZY = 3

    @property
    def label(self) -> str:
        return {
            MatchKind.TITLE_EXACT: "title",
            MatchKind.TITLE_FUZZY: "title (fuzzy)",
            MatchKind.CONTENT_EXACT: "content",
            MatchKind.CONTENT_FUZZY: "content (fuzzy)",
        }[self]


# A fuzzy (non-exact) hit must clear this normalized bar (score per query
# character) to count at all — otherwise a short/generic query fuzzy-matches
# scattered letters across nearly every transcript ever written, burying the
# few real matches under noise. An exact substring never needs this: it's
# never noise, no matter how deep it's buried — that's the "still find
# something very small" guarantee.
_MIN_FUZZY_SCORE_PER_CHAR = 0.0


def fuzzy_score(query: str, text: str) -> float | None:
    """fzf-style subsequence match: every query character must appear in
    `text`, in order, case-insensitively. None if it doesn't match at
    all. Higher is better — rewards contiguous runs and matches right
    after a word boundary, penalizes gaps between matched characters."""
    if not query:
        return None
    q = query.lower()
    t = text.lower()
    search_from = 0
    score = 0.0
    last_match = -1
    first_match = -1
    for ch in q:
        idx = t.find(ch, search_from)
        if idx == -1:
            return None
        if first_match == -1:
            first_match = idx
        if last_match != -1:
            gap = idx - last_match - 1
            score -= gap * 0.5
        if idx == 0 or t[idx - 1] in _WORD_BOUNDARY:
            score += 3.0
        if last_match == idx - 1:
            score += 2.0
        score += 1.0
        last_match = idx
        search_from = idx + 1
    # Earlier first match in a long line is usually the more relevant hit.
    score -= first_match * 0.001
    return score


@dataclass
class SearchResult:
    session_id: str
    project_dir: str
    title: str
    score: float
    kind: MatchKind
    snippet: str  # the single best-matching line, for context


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _match(query: str, text: str) -> tuple[float, bool] | None:
    """(score, is_exact) against one piece of text, or None for no match.

    An exact (case-insensitive) substring is checked first and, when
    found, scored on its own scale — always higher than any possible
    fuzzy score for the same query, so it never loses a same-tier
    comparison to a fuzzy hit. Falls back to the fzf-style subsequence
    matcher only when no literal substring exists."""
    if not query:
        return None
    lower_text = text.lower()
    idx = lower_text.find(query.lower())
    if idx != -1:
        boundary = idx == 0 or lower_text[idx - 1] in _WORD_BOUNDARY
        score = 1000.0 + len(query) + (5.0 if boundary else 0.0) - idx * 0.01 - len(text) * 0.001
        return score, True
    score = fuzzy_score(query, text)
    if score is None:
        return None
    return score, False


def _scan_transcript(path: Path) -> tuple[str, str, list[str]]:
    """(project_dir, title, text_lines) — a genuinely full read, every
    line, never head/tail-truncated. project_dir comes straight off the
    records' own "cwd" field (authoritative — no lossy path decoding);
    title prefers the latest ai-title record, falling back to the first
    user message."""
    project_dir = ""
    title = ""
    first_user_text = ""
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not project_dir:
                    cwd = record.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        project_dir = cwd
                rtype = record.get("type")
                if rtype == "ai-title":
                    ai_title = record.get("aiTitle")
                    if isinstance(ai_title, str) and ai_title.strip():
                        title = ai_title.strip()
                    continue
                if rtype not in ("user", "assistant"):
                    continue
                for block in _iter_content_blocks(record.get("message")):
                    btype = block.get("type")
                    if btype == "text":
                        text = str(block.get("text", "")).strip()
                    elif btype == "tool_result":
                        text = _result_text(block).strip()
                    else:
                        continue
                    if not text:
                        continue
                    lines.append(text)
                    if rtype == "user" and not first_user_text:
                        first_user_text = text
    except OSError:
        return project_dir, title, lines
    if not title:
        title = _truncate(first_user_text, 60)
    return project_dir, title, lines


def search_all_sessions(
    claude_dir: Path, query: str, limit: int = 50, sessions: list[DiscoveredSession] | None = None
) -> list[SearchResult]:
    """Every session transcript under claude_dir, fully read (not just
    discovered), scored against `query`, best matches first. Slow by
    design — this is the "even if it takes a while" full-history search,
    not the fast display path.

    Ranking is tiered, not a single flat score: a conversation's own name
    matching always outranks a body hit, and within either, an exact
    substring always outranks a fuzzy one — see MatchKind. Only within
    the same tier does score break the tie."""
    if not query.strip():
        return []
    if sessions is None:
        sessions = discover_sessions(claude_dir, min_size=1)
    results: list[SearchResult] = []
    for discovered in sessions:
        project_dir, title, lines = _scan_transcript(discovered.path)
        best: tuple[MatchKind, float, str] | None = None

        def consider(text: str, exact_kind: MatchKind, fuzzy_kind: MatchKind) -> None:
            nonlocal best
            match = _match(query, text)
            if match is None:
                return
            score, is_exact = match
            if not is_exact and score / len(query) <= _MIN_FUZZY_SCORE_PER_CHAR:
                return
            kind = exact_kind if is_exact else fuzzy_kind
            if best is None or (kind, -score) < (best[0], -best[1]):
                best = (kind, score, text)

        if title:
            consider(title, MatchKind.TITLE_EXACT, MatchKind.TITLE_FUZZY)
        for line in lines:
            consider(line, MatchKind.CONTENT_EXACT, MatchKind.CONTENT_FUZZY)

        if best is None:
            continue
        kind, score, snippet = best
        results.append(
            SearchResult(
                session_id=discovered.session_id,
                project_dir=project_dir,
                title=title or discovered.session_id[:8],
                score=score,
                kind=kind,
                snippet=_truncate(snippet, 140),
            )
        )
    results.sort(key=lambda r: (r.kind, -r.score))
    return results[:limit]
