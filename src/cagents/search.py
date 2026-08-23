"""Fuzzy full-text search across every Claude conversation transcript on
disk — the complete message history, not just titles or the display
preview (which deliberately only reads a head/tail window of large files
for speed; see claude_data.HEAD_BYTES/TAIL_BYTES). A full scan can be slow
across many/large transcripts, so this is opt-in (conversation_search
setting) and only runs when the user explicitly asks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .claude_data import DiscoveredSession, _iter_content_blocks, _result_text, discover_sessions

_WORD_BOUNDARY = set(" _-./:\n\t()[]{}")


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
    snippet: str  # the single best-matching line, for context


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
    not the fast display path."""
    if not query.strip():
        return []
    if sessions is None:
        sessions = discover_sessions(claude_dir, min_size=1)
    results: list[SearchResult] = []
    for discovered in sessions:
        project_dir, title, lines = _scan_transcript(discovered.path)
        best_score: float | None = None
        best_line = ""
        for line in lines:
            score = fuzzy_score(query, line)
            if score is not None and (best_score is None or score > best_score):
                best_score = score
                best_line = line
        # the title itself counts as searchable text too
        title_score = fuzzy_score(query, title)
        if title_score is not None and (best_score is None or title_score > best_score):
            best_score = title_score
            best_line = title
        if best_score is None:
            continue
        results.append(
            SearchResult(
                session_id=discovered.session_id,
                project_dir=project_dir,
                title=title or discovered.session_id[:8],
                score=best_score,
                snippet=_truncate(best_line, 140),
            )
        )
    results.sort(key=lambda r: -r.score)
    return results[:limit]
