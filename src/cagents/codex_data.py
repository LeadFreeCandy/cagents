"""Read-only, bounded parsing of Codex rollout transcripts.

Codex owns these files. Their format is not a stable API; ignore unknown
records and use explicit turn events when present. IDs are namespaced so
Claude and Codex bookkeeping cannot collide.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .claude_data import (
    DiscoveredSession, ParsedSession, PreviewItem, HEAD_BYTES, TAIL_BYTES,
    _first_line, _parse_ts, _read_lines,
)

_ROLLOUT_ID = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")


def default_codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def discover_sessions(codex_dir: Path, min_size: int = 1) -> list[DiscoveredSession]:
    found = {}
    for folder in ("sessions", "archived_sessions"):
        for path in (codex_dir / folder).rglob("*.jsonl"):
            match = _ROLLOUT_ID.search(path.stem)
            if not match:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size < min_size:
                continue
            sid = "codex:" + match[1]
            entry = DiscoveredSession(sid, path, "", stat.st_mtime, stat.st_size)
            if sid not in found or entry.mtime > found[sid].mtime:
                found[sid] = entry
    return sorted(found.values(), key=lambda s: s.mtime, reverse=True)


def message_text(payload: dict) -> str:
    content = payload.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict)
                     and isinstance(b.get("text"), str))


def records(path: Path, *, full: bool = False, head_bytes=HEAD_BYTES, tail_bytes=TAIL_BYTES):
    if full:
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield from _decode(stream)
    else:
        lines, _ = _read_lines(path, head_bytes, tail_bytes)
        yield from _decode(lines)


def _decode(lines):
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and isinstance(record.get("payload"), dict):
            yield record


def parse_session_file(path: Path, head_bytes=HEAD_BYTES, tail_bytes=TAIL_BYTES,
                       preview_items=60) -> ParsedSession:
    stat = path.stat()
    match = _ROLLOUT_ID.search(path.stem)
    parsed = ParsedSession("codex:" + (match[1] if match else path.stem), path,
                           mtime=stat.st_mtime, size=stat.st_size,
                           truncated=stat.st_size > head_bytes + tail_bytes)
    preview = []
    pending = {}
    for record in records(path, head_bytes=head_bytes, tail_bytes=tail_bytes):
        kind, p = record.get("type"), record["payload"]
        ts = _parse_ts(record.get("timestamp"))
        typ = p.get("type")
        if kind == "session_meta":
            sid = p.get("id") or p.get("session_id")
            if isinstance(sid, str):
                parsed.session_id = "codex:" + sid
            parsed.cwd = str(p.get("cwd") or "")
            parsed.last_cwd = parsed.cwd
            parsed.version = str(p.get("cli_version") or "")
            parsed.first_timestamp = _parse_ts(p.get("timestamp")) or ts
            git = p.get("git") or {}
            if isinstance(git, dict):
                parsed.git_branch = str(git.get("branch") or "")
        elif kind == "turn_context":
            parsed.last_cwd = str(p.get("cwd") or parsed.last_cwd)
            parsed.model = str(p.get("model") or parsed.model)
        elif kind == "event_msg":
            if typ in ("task_started", "turn_started"):
                parsed.turn_state = "running"
                parsed.last_timestamp = ts or parsed.last_timestamp
                pending.clear()
            elif typ in ("task_complete", "task_completed", "turn_complete", "turn_completed"):
                parsed.turn_state = "completed"
                parsed.last_stop_reason = "end_turn"
                parsed.last_record_role = "assistant"
                parsed.last_timestamp = ts or parsed.last_timestamp
                parsed.last_turn_duration_ms = p.get("duration_ms") or 0
                pending.clear()
            elif typ == "turn_aborted":
                parsed.turn_state = "interrupted"
                parsed.last_timestamp = ts or parsed.last_timestamp
                pending.clear()
            # Older rollouts carry user/assistant messages as event payloads.
            elif typ in ("user_message", "agent_message"):
                text = str(p.get("message") or "")
                role = "user" if typ == "user_message" else "assistant"
                _message(parsed, preview, role, text, ts)
        elif kind == "response_item":
            if typ == "message" and p.get("role") in ("user", "assistant"):
                _message(parsed, preview, p["role"], message_text(p), ts)
                if p.get("phase") == "final_answer":
                    parsed.turn_state = "completed"
                    pending.clear()
            elif typ in ("function_call", "custom_tool_call"):
                name = str(p.get("name") or "tool")
                pending[str(p.get("call_id") or name)] = name
                text = str(p.get("arguments") or p.get("input") or "")
                preview.append(PreviewItem("tool", _first_line(text), ts, name))
                parsed.turn_state = "running"
                parsed.last_timestamp = ts or parsed.last_timestamp
            elif typ in ("function_call_output", "custom_tool_call_output"):
                pending.pop(str(p.get("call_id")), None)
                parsed.turn_state = "running"
                parsed.last_timestamp = ts or parsed.last_timestamp
            elif typ == "reasoning":
                parsed.turn_state = "running"
                parsed.last_timestamp = ts or parsed.last_timestamp
        elif kind == "compacted":
            parsed.compact_count += 1
    parsed.pending_tool_use = bool(pending)
    parsed.pending_tool_name = next(reversed(pending.values()), "")
    parsed.preview = preview[-preview_items:]
    parsed.title = parsed.title or parsed.session_id.removeprefix("codex:")[:8]
    return parsed


def _message(parsed, preview, role, text, ts):
    if not text.strip():
        return
    # Configuration context is stored as user messages but isn't a user task.
    if role == "user" and text.lstrip().startswith(("<environment_context>", "# AGENTS.md instructions")):
        return
    if not preview or (preview[-1].kind, preview[-1].text) != (role, text):
        preview.append(PreviewItem(role, text, ts))
    if role == "user":
        parsed.title = parsed.title or _first_line(text, 80)
        parsed.turn_state = "running"
    else:
        parsed.last_assistant_text = _first_line(text)
        parsed.turn_state = "running"
    parsed.last_record_role = role
    parsed.last_timestamp = ts or parsed.last_timestamp
    parsed.first_timestamp = parsed.first_timestamp or ts


def scan_transcript(path: Path) -> tuple[str, str, list[str]]:
    parsed = parse_session_file(path)
    lines = []
    for record in records(path, full=True):
        p = record["payload"]
        if record.get("type") == "response_item" and p.get("role") in ("user", "assistant"):
            lines.append(message_text(p))
        elif record.get("type") == "event_msg" and p.get("type") in ("user_message", "agent_message"):
            lines.append(str(p.get("message") or ""))
    return parsed.cwd, parsed.title, lines
