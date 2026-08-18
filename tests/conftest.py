"""Shared fixtures: builders that write session transcripts in the exact
shape Claude Code writes them (verified against real ~/.claude data)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cagents.claude_data import encode_project_dir


class TranscriptBuilder:
    """Builds a session .jsonl the way Claude Code writes one."""

    def __init__(self, session_id: str, cwd: str, git_branch: str = "main"):
        self.session_id = session_id
        self.cwd = cwd
        self.git_branch = git_branch
        self.lines: list[str] = []
        self._uuid_n = 0
        self._last_uuid: str | None = None

    def _uuid(self) -> str:
        self._uuid_n += 1
        return f"00000000-0000-0000-0000-{self._uuid_n:012d}"

    def _base(self, ts: str) -> dict:
        uuid = self._uuid()
        record = {
            "parentUuid": self._last_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": "2.1.234",
            "gitBranch": self.git_branch,
            "uuid": uuid,
            "timestamp": ts,
        }
        self._last_uuid = uuid
        return record

    def raw(self, obj: dict) -> "TranscriptBuilder":
        self.lines.append(json.dumps(obj))
        return self

    def ai_title(self, title: str) -> "TranscriptBuilder":
        return self.raw({"type": "ai-title", "aiTitle": title, "sessionId": self.session_id})

    def user(self, text: str, ts: str = "2026-08-17T10:00:00.000Z") -> "TranscriptBuilder":
        record = self._base(ts)
        record.update({"type": "user", "message": {"role": "user", "content": text}})
        return self.raw(record)

    def assistant_text(
        self, text: str, ts: str = "2026-08-17T10:00:05.000Z", stop_reason: str = "end_turn"
    ) -> "TranscriptBuilder":
        record = self._base(ts)
        record.update(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": stop_reason,
                },
            }
        )
        return self.raw(record)

    def assistant_thinking(self, text: str, ts: str = "2026-08-17T10:00:03.000Z") -> "TranscriptBuilder":
        record = self._base(ts)
        record.update(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": [{"type": "thinking", "thinking": text}],
                    "stop_reason": "tool_use",
                },
            }
        )
        return self.raw(record)

    def assistant_tool_use(
        self,
        tool_id: str,
        name: str,
        tool_input: dict,
        ts: str = "2026-08-17T10:00:06.000Z",
    ) -> "TranscriptBuilder":
        record = self._base(ts)
        record.update(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": [
                        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
                    ],
                    "stop_reason": "tool_use",
                },
            }
        )
        return self.raw(record)

    def tool_result(self, tool_id: str, ts: str = "2026-08-17T10:00:08.000Z") -> "TranscriptBuilder":
        record = self._base(ts)
        record.update(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
                    ],
                },
            }
        )
        return self.raw(record)

    def sidechain_user(self, text: str, ts: str = "2026-08-17T10:00:09.000Z") -> "TranscriptBuilder":
        record = self._base(ts)
        record["isSidechain"] = True
        record.update({"type": "user", "message": {"role": "user", "content": text}})
        return self.raw(record)

    def write(self, claude_dir: Path, mtime: float | None = None) -> Path:
        project = claude_dir / "projects" / encode_project_dir(self.cwd)
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{self.session_id}.jsonl"
        path.write_text("\n".join(self.lines) + "\n", "utf-8")
        if mtime is not None:
            import os

            os.utime(path, (mtime, mtime))
        return path


@pytest.fixture
def claude_dir(tmp_path: Path) -> Path:
    d = tmp_path / "claude"
    (d / "projects").mkdir(parents=True)
    return d


@pytest.fixture
def now() -> float:
    return time.time()


SID1 = "11111111-1111-1111-1111-111111111111"
SID2 = "22222222-2222-2222-2222-222222222222"
SID3 = "33333333-3333-3333-3333-333333333333"


def ts_ago(seconds: float) -> str:
    """ISO timestamp `seconds` ago — for tests that mean 'recent activity'.
    (State freshness runs on record timestamps, not file mtime.)"""
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    return _dt.fromtimestamp(_time.time() - seconds, tz=_tz.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
