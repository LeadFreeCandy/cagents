"""Tests for reading Claude Code's session store."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from conftest import SID1, SID2, TranscriptBuilder

from cagents.claude_data import (
    discover_sessions,
    encode_project_dir,
    parse_session_file,
    session_file_path,
)


def test_encode_project_dir_matches_claude_scheme():
    assert encode_project_dir("/Users/samir/Documents/projects/cagents") == (
        "-Users-samir-Documents-projects-cagents"
    )
    # Lossy chars all become dashes
    assert encode_project_dir("/tmp/my_proj.x") == "-tmp-my-proj-x"


def test_session_file_path(claude_dir: Path):
    p = session_file_path(claude_dir, "/a/b", SID1)
    assert p == claude_dir / "projects" / "-a-b" / f"{SID1}.jsonl"


def test_parse_basic_conversation(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha", git_branch="feat-x")
    b.ai_title("Fix the login bug")
    b.user("please fix the login bug", ts="2026-08-17T10:00:00.000Z")
    b.assistant_thinking("Let me look at auth.py first")
    b.assistant_tool_use("t1", "Read", {"file_path": "/proj/alpha/auth.py"})
    b.tool_result("t1")
    b.assistant_text("Fixed: the token check was inverted.", ts="2026-08-17T10:01:00.000Z")
    path = b.write(claude_dir)

    parsed = parse_session_file(path)
    assert parsed.session_id == SID1
    assert parsed.title == "Fix the login bug"
    assert parsed.cwd == "/proj/alpha"
    assert parsed.git_branch == "feat-x"
    assert parsed.model == "claude-fable-5"
    assert parsed.first_timestamp == datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
    assert parsed.last_timestamp == datetime(2026, 8, 17, 10, 1, 0, tzinfo=timezone.utc)
    assert parsed.last_stop_reason == "end_turn"
    assert parsed.pending_tool_use is False
    assert parsed.last_record_role == "assistant"

    kinds = [i.kind for i in parsed.preview]
    assert kinds == ["user", "thinking", "tool", "assistant"]
    tool = parsed.preview[2]
    assert tool.tool_name == "Read"
    assert "auth.py" in tool.text


def test_pending_tool_use_detected(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("run the tests")
    b.assistant_tool_use("t9", "Bash", {"command": "pytest -x"})
    path = b.write(claude_dir)

    parsed = parse_session_file(path)
    assert parsed.pending_tool_use is True
    assert parsed.pending_tool_name == "Bash"
    # A resolved tool_use is not pending
    b.tool_result("t9")
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.pending_tool_use is False
    assert parsed.last_record_role == "user"


def test_manual_rename_in_claude_code_overrides_ai_title(claude_dir: Path):
    # Renaming the conversation IN Claude Code (not cagents' own `r`) writes
    # its own "custom-title" record — must win over (and, if it comes later,
    # replace) the auto-generated aiTitle.
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.ai_title("Fix the login bug")
    b.user("please fix the login bug")
    b.custom_title("Auth token bug")
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.title == "Auth token bug"


def test_ai_title_after_a_custom_title_does_not_override_it(claude_dir: Path):
    # A later auto-title regeneration must not clobber an explicit rename —
    # customTitle wins within the same record even if aiTitle also updates.
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.custom_title("Auth token bug")
    b.user("please fix the login bug")
    b.raw({"type": "ai-title", "aiTitle": "Fix the login bug", "customTitle": "Auth token bug"})
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.title == "Auth token bug"


def test_custom_title_survives_a_later_ai_title_record(claude_dir: Path):
    # Claude writes BOTH records on every save — the custom-title first, then
    # a plain ai-title right behind it. Whichever came last must not decide
    # the title, or every renamed session reverts to its generated one.
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.custom_title("osg-converter")
    b.user("keep going")
    b.ai_title("Review osg-converter project and restart testing framework")
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.title == "osg-converter"


def test_title_fallback_to_first_user_message(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("refactor the database layer\nwith more detail here")
    b.assistant_text("On it.")
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.title == "refactor the database layer"


def test_sidechain_and_systemish_records_excluded(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("real question")
    b.sidechain_user("subagent chatter")
    b.user("<system-reminder>noise</system-reminder>")
    b.user("<task-notification>done</task-notification>")
    b.assistant_text("answer")
    parsed = parse_session_file(b.write(claude_dir))
    texts = [(i.kind, i.text) for i in parsed.preview]
    assert texts == [("user", "real question"), ("assistant", "answer")]


def test_is_meta_user_records_excluded_from_preview_and_title(claude_dir: Path):
    # Real bug, confirmed live against actual transcripts: skill-injected
    # instructions and pasted-image placeholders arrive as "isMeta":true
    # user blocks with none of _SYSTEMISH_USER's structural prefixes — they
    # were read as real user text, becoming the fallback title and getting
    # rendered into the preview pane as if typed.
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("Base directory for this skill: /Users/x/.claude/skills/walkthrough", is_meta=True)
    b.user("real question")
    b.assistant_text("answer")
    parsed = parse_session_file(b.write(claude_dir))
    texts = [(i.kind, i.text) for i in parsed.preview]
    assert texts == [("user", "real question"), ("assistant", "answer")]
    assert parsed.title == "real question"


def test_compact_boundary_tracked(claude_dir: Path):
    # Confirmed live against real transcripts: auto-compaction writes
    # {"type":"system","subtype":"compact_boundary","compactMetadata":
    # {"cumulativeDroppedTokens":...}} — cagents previously never read it.
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("go")
    b.raw(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": SID1,
            "compactMetadata": {"trigger": "auto", "cumulativeDroppedTokens": 907269},
        }
    )
    b.assistant_text("continuing")
    b.raw(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": SID1,
            "compactMetadata": {"trigger": "auto", "cumulativeDroppedTokens": 1200000},
        }
    )
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.compact_count == 2
    assert parsed.compacted_tokens == 1200000


def test_tolerates_garbage_lines(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("hello")
    b.lines.append("this is not json {{{")
    b.lines.append(json.dumps(["not", "a", "dict"]))
    b.assistant_text("hi")
    parsed = parse_session_file(b.write(claude_dir))
    assert [i.kind for i in parsed.preview] == ["user", "assistant"]


def test_large_file_reads_head_and_tail(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.ai_title("Big session")
    b.user("first message", ts="2026-08-17T09:00:00.000Z")
    filler = "x" * 500
    for i in range(200):
        b.assistant_text(f"{filler} step {i}", ts=f"2026-08-17T09:{i % 60:02d}:30.000Z")
    b.assistant_text("the final answer", ts="2026-08-17T10:30:00.000Z")
    path = b.write(claude_dir)

    parsed = parse_session_file(path, head_bytes=4096, tail_bytes=8192)
    assert parsed.truncated is True
    # Head metadata survived
    assert parsed.title == "Big session"
    assert parsed.cwd == "/proj/alpha"
    assert parsed.first_timestamp is not None
    assert parsed.first_timestamp.hour == 9
    # Tail state survived
    assert parsed.preview[-1].text == "the final answer"
    assert parsed.last_timestamp == datetime(2026, 8, 17, 10, 30, 0, tzinfo=timezone.utc)


def test_preview_is_bounded(claude_dir: Path):
    b = TranscriptBuilder(SID1, "/proj/alpha")
    for i in range(100):
        b.user(f"msg {i}")
    parsed = parse_session_file(b.write(claude_dir), preview_items=10)
    assert len(parsed.preview) == 10
    assert parsed.preview[-1].text == "msg 99"


def test_string_content_message(claude_dir: Path):
    """Real transcripts carry plain-string content for user messages."""
    b = TranscriptBuilder(SID1, "/proj/alpha")
    b.user("plain string content")
    parsed = parse_session_file(b.write(claude_dir))
    assert parsed.preview[0].text == "plain string content"


def test_discover_sessions_orders_newest_first(claude_dir: Path, now: float):
    TranscriptBuilder(SID1, "/proj/alpha").user("a").write(claude_dir, mtime=now - 100)
    TranscriptBuilder(SID2, "/proj/beta").user("b").write(claude_dir, mtime=now - 10)
    found = discover_sessions(claude_dir)
    assert [s.session_id for s in found] == [SID2, SID1]
    assert found[0].encoded_project == "-proj-beta"


def test_discover_handles_missing_dir(tmp_path: Path):
    assert discover_sessions(tmp_path / "nope") == []
