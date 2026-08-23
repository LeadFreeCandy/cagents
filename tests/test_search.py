"""Fuzzy full-text search across the complete conversation history —
never the head/tail-truncated path parse_session_file uses for display."""

from __future__ import annotations

from pathlib import Path

from conftest import SID1, SID2, SID3, TranscriptBuilder

from cagents.claude_data import DiscoveredSession
from cagents.search import fuzzy_score, search_all_sessions


# --------------------------------------------------------------- scoring ---


def test_fuzzy_score_requires_subsequence_in_order():
    assert fuzzy_score("abc", "xaxbxc") is not None
    assert fuzzy_score("abc", "cba") is None  # wrong order
    assert fuzzy_score("xyz", "abc") is None  # not present at all
    assert fuzzy_score("", "anything") is None


def test_fuzzy_score_rewards_contiguous_and_word_boundary_matches():
    # "fix" contiguous and at a word boundary should outscore the same
    # letters scattered across unrelated words.
    tight = fuzzy_score("fix", "please fix the bug")
    scattered = fuzzy_score("fix", "f-l-o-w i-n-d-e-x")
    assert tight is not None and scattered is not None
    assert tight > scattered


def test_fuzzy_score_case_insensitive():
    assert fuzzy_score("FIX", "a fix landed") is not None


# ------------------------------------------------------- full-scan search ---


def test_search_finds_a_match_buried_deep_in_a_long_transcript(claude_dir: Path):
    # The whole point: parse_session_file only reads a head/tail window
    # for display speed. A real full-history search must not have that
    # blind spot — plant the needle in the middle of many filler messages.
    b = TranscriptBuilder(SID1, "/proj/alpha").ai_title("Alpha work")
    for i in range(300):
        b.user(f"filler message number {i} about nothing in particular")
        if i == 150:
            b.assistant_text("the unobtainium widget needs recalibration")
        else:
            b.assistant_text(f"ok, filler reply {i}")
    b.write(claude_dir)

    results = search_all_sessions(claude_dir, "unobtainium")
    assert len(results) == 1
    assert results[0].session_id == SID1
    assert "unobtainium" in results[0].snippet


def test_search_ranks_better_matches_first(claude_dir: Path):
    TranscriptBuilder(SID1, "/proj/a").ai_title("A").user("go").assistant_text(
        "totally unrelated text about gardening"
    ).write(claude_dir)
    TranscriptBuilder(SID2, "/proj/b").ai_title("B").user("go").assistant_text(
        "fix the login bug in auth.py"
    ).write(claude_dir)
    TranscriptBuilder(SID3, "/proj/c").ai_title("C").user("go").assistant_text(
        "before I explain xerox and other unrelated topics entirely"
    ).write(claude_dir)

    results = search_all_sessions(claude_dir, "fix")
    ids = [r.session_id for r in results]
    assert SID1 not in ids  # no match at all
    assert ids[0] == SID2  # tight, word-boundary match ranks above scattered


def test_search_matches_against_the_title_too(claude_dir: Path):
    TranscriptBuilder(SID1, "/proj/a").ai_title("Refactor the owner packet pipeline").user(
        "go"
    ).assistant_text("done").write(claude_dir)
    results = search_all_sessions(claude_dir, "owner packet")
    assert [r.session_id for r in results] == [SID1]


def test_search_empty_query_returns_nothing(claude_dir: Path):
    TranscriptBuilder(SID1, "/proj/a").user("go").assistant_text("done").write(claude_dir)
    assert search_all_sessions(claude_dir, "") == []
    assert search_all_sessions(claude_dir, "   ") == []


def test_search_uses_the_records_own_cwd_not_a_decoded_path(claude_dir: Path):
    TranscriptBuilder(SID1, "/proj/weird-dir_name").user("go").assistant_text(
        "the target phrase"
    ).write(claude_dir)
    results = search_all_sessions(claude_dir, "target phrase")
    assert results[0].project_dir == "/proj/weird-dir_name"


def test_search_skips_unreadable_transcript_without_raising(claude_dir: Path, tmp_path: Path):
    project = claude_dir / "projects" / "-proj-a"
    project.mkdir(parents=True, exist_ok=True)
    ghost = DiscoveredSession(
        session_id="ghost", path=project / "ghost.jsonl", encoded_project="-proj-a",
        mtime=0.0, size=1,
    )
    assert search_all_sessions(claude_dir, "anything", sessions=[ghost]) == []
