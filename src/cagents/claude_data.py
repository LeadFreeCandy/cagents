"""Read-only access to Claude Code's own session store (~/.claude/projects).

cagents never writes here. Everything in this module is derived from what
Claude Code already persists: one JSONL transcript per session, stored under
a directory whose name encodes the project path.

Large transcripts are never read in full: we read a bounded head (for session
metadata like cwd and start time) and a bounded tail (for the recent
conversation and turn-state signals).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# How much of a transcript we read. Head covers session metadata (cwd shows up
# on the first user/assistant record); tail covers the preview and turn state.
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024

_NON_PATH_CHARS = re.compile(r"[^A-Za-z0-9-]")


def encode_project_dir(path: str) -> str:
    """Encode a project path the way Claude Code names its per-project dirs.

    e.g. /Users/samir/Documents/my_proj -> -Users-samir-Documents-my-proj
    The encoding is lossy; cagents only ever uses it to *locate* a session
    file from a known real path, never to reverse it.
    """
    return _NON_PATH_CHARS.sub("-", path)


def default_claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def session_file_path(claude_dir: Path, project_dir: str, session_id: str) -> Path:
    return claude_dir / "projects" / encode_project_dir(project_dir) / f"{session_id}.jsonl"


@dataclass
class PreviewItem:
    """One renderable line-group of the conversation tail."""

    kind: str  # "user" | "assistant" | "tool" | "thinking"
    text: str
    timestamp: datetime | None = None
    # For kind == "tool": the tool name (text carries a short input summary).
    tool_name: str = ""


@dataclass
class Link:
    """Something linkable that Claude recorded in the transcript."""

    kind: str  # "pr" | "artifact" | ...
    label: str
    url: str


# "Claude writes it, cagents shows it": record types that carry a link.
# Growing with Claude means adding one entry here when a new *-link record
# type appears in transcripts — nothing else changes.
_LINK_EXTRACTORS = {
    "pr-link": lambda r: Link(
        kind="pr",
        label=f"PR #{r['prNumber']}" if r.get("prNumber") else "PR",
        url=str(r.get("prUrl", "")),
    ),
    "frame-link": lambda r: Link(
        kind="artifact",
        label="artifact",
        url=str(r.get("frameUrl", "")),
    ),
}

# Tool calls that modify files, and where the path lives in their input.
_FILE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


@dataclass
class ParsedSession:
    session_id: str
    path: Path
    cwd: str = ""  # first cwd seen — stable, used for grouping
    last_cwd: str = ""  # latest cwd seen — follows EnterWorktree etc.
    git_branch: str = ""
    title: str = ""
    model: str = ""
    version: str = ""
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    mtime: float = 0.0
    size: int = 0
    preview: list[PreviewItem] = field(default_factory=list)
    # Turn-state signals, all derived from the transcript tail:
    last_stop_reason: str = ""  # stop_reason of the last assistant record seen
    # True when the newest assistant tool_use has no later tool_result — i.e.
    # a tool call is either still running or waiting on a permission decision.
    pending_tool_use: bool = False
    pending_tool_name: str = ""
    last_record_role: str = ""  # "user" | "assistant" | "" if neither seen
    truncated: bool = False  # tail parse did not cover the whole file
    # First line of the newest assistant text — "what the agent last did/said".
    last_assistant_text: str = ""
    # Long-lived side tasks (lifecycle verified against real transcripts).
    # A backgrounded command starts with a "running in background with
    # ID: <id>" ack and ends with a <task-notification> carrying that
    # <task-id> and a terminal <status>; a Monitor starts with
    # "Monitor started (task <id>, timeout <N>ms)" and ends on its timeout
    # notification — or, as a hard upper bound, when the timeout elapses.
    # Sending a new message does NOT stop them, so nothing here resets on
    # human input.
    background_active: bool = False  # any background command still running
    monitor_expiries: list[float] = field(default_factory=list)  # epoch deadlines

    def monitor_running(self, now: float) -> bool:
        return any(deadline > now for deadline in self.monitor_expiries)
    # Derived extras, all read straight from records Claude already writes:
    links: list[Link] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)  # ordered, deduped
    pending_agents: int = 0  # background agents, per Claude's system records
    last_turn_duration_ms: int = 0
    # How many times this session's history has been auto/manually compacted
    # (a "system"/"compact_boundary" record), and the cumulative tokens
    # dropped across all of them — confirmed live in real transcripts as
    # {"type":"system","subtype":"compact_boundary","compactMetadata":
    # {"cumulativeDroppedTokens":...}}. Explains why old context vanishes
    # from the preview/title derivation without cagents ever seeing an
    # error.
    compact_count: int = 0
    compacted_tokens: int = 0


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_line(text: str, limit: int = 200) -> str:
    line = text.strip().split("\n", 1)[0].strip()
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line


def _summarize_tool_input(name: str, tool_input: object) -> str:
    """A one-line human summary of a tool call's input."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "description", "file_path", "pattern", "prompt", "url", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _first_line(value, 120)
    return ""


def _result_text(block: dict) -> str:
    """Flatten a tool_result's content to text (string or block list)."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _iter_content_blocks(message: object):
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield {"type": "text", "text": content}
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


_BG_ACK = re.compile(r"running in background with ID:\s*(\S+?)\.?(?:\s|$)")
_MONITOR_ACK = re.compile(r"Monitor started \(task (\S+), timeout (\d+)ms\)")
_TASK_NOTIF_ID = re.compile(r"<task-id>(\S+)</task-id>")
_TASK_TERMINAL = re.compile(r"<status>(?:completed|failed|killed)</status>")
_MONITOR_TIMED_OUT = re.compile(r"\[Monitor timed out")


def _scan_lifecycle(text: str, ts, active_background: dict, active_monitors: dict) -> None:
    """Track long-lived side tasks through their transcript lifecycle."""
    match = _BG_ACK.search(text)
    if match:
        active_background[match.group(1).rstrip(".")] = True
    match = _MONITOR_ACK.search(text)
    if match and ts is not None:
        active_monitors[match.group(1)] = ts.timestamp() + int(match.group(2)) / 1000.0
    match = _TASK_NOTIF_ID.search(text)
    if match:
        task_id = match.group(1)
        if _TASK_TERMINAL.search(text):
            # A normal Monitor completion notification carries the exact
            # same <status>completed</status> shape as a background
            # command's (confirmed live: task-notification for a Monitor
            # that finished on its own, not via timeout, reads
            # "<status>completed</status>" same as background). Popping
            # both dicts unconditionally is a harmless no-op for whichever
            # one this task_id doesn't belong to.
            active_background.pop(task_id, None)
            active_monitors.pop(task_id, None)
        if _MONITOR_TIMED_OUT.search(text):
            active_monitors.pop(task_id, None)


# Fallback for the handful of harness-synthesized shapes seen without a
# structural marker. The primary signal is the record's own "isMeta" field
# (checked directly at the call site) — confirmed live: 172 "isMeta":true
# user blocks across real transcripts (skill-injected instructions, pasted
# image placeholders like "[Image: source: ...]") matched none of these
# prefixes, so they were read as real user text — became the fallback
# title and, worse, rendered straight into the preview pane as if typed.
_SYSTEMISH_USER = re.compile(
    r"^\s*(<system-reminder>|<task-notification>|<local-command|<command-name>|\[Request interrupted)"
)


def _read_lines(path: Path, head_bytes: int, tail_bytes: int) -> tuple[list[str], bool]:
    """Read the transcript as (lines, truncated).

    For small files this is every line. For large ones it is the head lines
    followed by the tail lines, with the seam marked by `truncated=True`
    (head metadata and tail preview/state never straddle the seam in
    practice, since metadata lives on the first records and state on the
    last).
    """
    size = path.stat().st_size
    with path.open("rb") as f:
        if size <= head_bytes + tail_bytes:
            data = f.read()
            return data.decode("utf-8", "replace").splitlines(), False
        head = f.read(head_bytes)
        f.seek(size - tail_bytes)
        tail = f.read()
    head_lines = head.decode("utf-8", "replace").splitlines()[:-1]  # drop partial
    tail_lines = tail.decode("utf-8", "replace").splitlines()[1:]  # drop partial
    return head_lines + tail_lines, True


def parse_session_file(
    path: Path,
    head_bytes: int = HEAD_BYTES,
    tail_bytes: int = TAIL_BYTES,
    preview_items: int = 60,
) -> ParsedSession:
    """Parse one session transcript into everything cagents displays.

    Raises OSError if the file cannot be read.
    """
    stat = path.stat()
    parsed = ParsedSession(
        session_id=path.stem,
        path=path,
        mtime=stat.st_mtime,
        size=stat.st_size,
    )
    lines, parsed.truncated = _read_lines(path, head_bytes, tail_bytes)

    preview: list[PreviewItem] = []
    # tool_use id -> preview index, so tool_results can be matched up.
    open_tool_uses: dict[str, str] = {}  # id -> tool name
    fallback_title = ""
    last_prompt = ""
    seen_links: set[str] = set()
    seen_files: set[str] = set()
    background_tool_ids: set[str] = set()  # tool_use ids with run_in_background
    active_background: dict[str, bool] = {}  # task id -> running
    active_monitors: dict[str, float] = {}  # task id -> expiry epoch

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        rtype = record.get("type")

        if rtype == "ai-title":
            # Renaming the conversation IN Claude Code (not cagents' own `r`)
            # writes a "customTitle" field on this same record type, sitting
            # alongside its auto-generated "aiTitle" — confirmed against the
            # installed `claude` binary's own bundled source, which reads
            # its title back the same way (a `customTitleFromTail` scan for
            # `"customTitle":"..."`). Only ever checking aiTitle here meant
            # a manual rename in Claude Code never reached cagents at all.
            # Last record chronologically wins, same as aiTitle always did;
            # customTitle wins within one record since it's the deliberate
            # override.
            custom = record.get("customTitle")
            if isinstance(custom, str) and custom.strip():
                parsed.title = custom.strip()
            else:
                title = record.get("aiTitle")
                if isinstance(title, str) and title.strip():
                    parsed.title = title.strip()
            continue
        if rtype == "agent-name":
            name = record.get("agentName")
            if isinstance(name, str) and name.strip() and not parsed.title:
                parsed.title = name.strip()
            continue
        if rtype == "summary":
            text = record.get("summary")
            if isinstance(text, str) and text.strip() and not parsed.title:
                parsed.title = text.strip()
            continue
        if rtype == "last-prompt":
            text = record.get("lastPrompt")
            if isinstance(text, str):
                last_prompt = text
            continue

        extractor = _LINK_EXTRACTORS.get(rtype)
        if extractor is not None:
            try:
                link = extractor(record)
            except (KeyError, TypeError):
                link = None
            if link is not None and link.url and link.url not in seen_links:
                seen_links.add(link.url)
                parsed.links.append(link)
            continue

        if rtype == "queue-operation":
            content = record.get("content")
            if isinstance(content, str):
                _scan_lifecycle(
                    content, _parse_ts(record.get("timestamp")),
                    active_background, active_monitors,
                )
            continue

        if rtype == "system":
            agents = record.get("pendingBackgroundAgentCount")
            if isinstance(agents, int):
                parsed.pending_agents = agents
            subtype = record.get("subtype")
            if subtype == "turn_duration":
                duration = record.get("durationMs")
                if isinstance(duration, int):
                    parsed.last_turn_duration_ms = duration
            elif subtype == "compact_boundary":
                parsed.compact_count += 1
                meta = record.get("compactMetadata")
                if isinstance(meta, dict):
                    dropped = meta.get("cumulativeDroppedTokens")
                    if isinstance(dropped, int):
                        parsed.compacted_tokens = dropped
            continue

        if rtype not in ("user", "assistant"):
            continue
        if record.get("isSidechain"):
            continue

        ts = _parse_ts(record.get("timestamp"))
        if ts is not None:
            if parsed.first_timestamp is None:
                parsed.first_timestamp = ts
            parsed.last_timestamp = ts

        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            parsed.cwd = parsed.cwd or cwd
            parsed.last_cwd = cwd
        branch = record.get("gitBranch")
        if isinstance(branch, str) and branch:
            parsed.git_branch = branch
        version = record.get("version")
        if isinstance(version, str) and version:
            parsed.version = version

        message = record.get("message")

        if rtype == "user":
            parsed.last_record_role = "user"
            is_meta = bool(record.get("isMeta"))
            for block in _iter_content_blocks(message):
                btype = block.get("type")
                if btype == "tool_result":
                    tool_id = block.get("tool_use_id")
                    if isinstance(tool_id, str):
                        open_tool_uses.pop(tool_id, None)
                        _scan_lifecycle(
                            _result_text(block), ts, active_background, active_monitors
                        )
                elif btype == "text":
                    text = block.get("text", "")
                    if not isinstance(text, str):
                        continue
                    if is_meta or _SYSTEMISH_USER.match(text):
                        _scan_lifecycle(text, ts, active_background, active_monitors)
                    elif text.strip():
                        if not fallback_title:
                            fallback_title = _first_line(text, 80)
                        preview.append(PreviewItem("user", text.strip(), ts))
            continue

        # assistant
        parsed.last_record_role = "assistant"
        if isinstance(message, dict):
            stop = message.get("stop_reason")
            if isinstance(stop, str) and stop:
                parsed.last_stop_reason = stop
            model = message.get("model")
            if isinstance(model, str) and model:
                parsed.model = model
        for block in _iter_content_blocks(message):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    preview.append(PreviewItem("assistant", text.strip(), ts))
                    parsed.last_assistant_text = _first_line(text, 110)
            elif btype == "thinking":
                text = block.get("thinking", "")
                if isinstance(text, str) and text.strip():
                    preview.append(PreviewItem("thinking", _first_line(text, 160), ts))
            elif btype == "tool_use":
                name = block.get("name", "")
                if not isinstance(name, str):
                    name = ""
                tool_id = block.get("id")
                if isinstance(tool_id, str):
                    open_tool_uses[tool_id] = name
                tool_input = block.get("input")
                if (
                    isinstance(tool_input, dict)
                    and tool_input.get("run_in_background")
                    and isinstance(tool_id, str)
                ):
                    background_tool_ids.add(tool_id)
                path_key = _FILE_TOOLS.get(name)
                if path_key and isinstance(tool_input, dict):
                    file_path = tool_input.get(path_key)
                    if isinstance(file_path, str) and file_path and file_path not in seen_files:
                        seen_files.add(file_path)
                        parsed.files_touched.append(file_path)
                summary = _summarize_tool_input(name, tool_input)
                preview.append(PreviewItem("tool", summary, ts, tool_name=name))

    if not parsed.title:
        parsed.title = fallback_title or _first_line(last_prompt, 80) or parsed.session_id[:8]

    parsed.background_active = bool(active_background)
    parsed.monitor_expiries = sorted(active_monitors.values())

    if open_tool_uses:
        parsed.pending_tool_use = True
        # The most recently opened tool call is the interesting one.
        parsed.pending_tool_name = next(reversed(open_tool_uses.values()))

    parsed.preview = preview[-preview_items:]
    return parsed


@dataclass
class DiscoveredSession:
    """A session found in Claude's store that cagents may not be tracking."""

    session_id: str
    path: Path
    encoded_project: str
    mtime: float
    size: int


def discover_sessions(claude_dir: Path, min_size: int = 1) -> list[DiscoveredSession]:
    """List every session transcript in Claude's store, newest first."""
    projects = claude_dir / "projects"
    found: list[DiscoveredSession] = []
    if not projects.is_dir():
        return found
    for project in projects.iterdir():
        if not project.is_dir():
            continue
        try:
            entries = list(project.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix != ".jsonl":
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            if stat.st_size < min_size:
                continue
            found.append(
                DiscoveredSession(
                    session_id=entry.stem,
                    path=entry,
                    encoded_project=project.name,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                )
            )
    found.sort(key=lambda s: s.mtime, reverse=True)
    return found


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
