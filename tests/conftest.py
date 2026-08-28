"""Shared fixtures: builders that write session transcripts in the exact
shape Claude Code writes them (verified against real ~/.claude data)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from cagents.claude_data import encode_project_dir


def init_git_repo(path: Path) -> None:
    """A minimal real git repo at `path` — for tests exercising the
    worktree-detection that gates terminal-tab access."""
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("# repo\n", "utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=path, capture_output=True, check=True
    )


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

    def custom_title(self, title: str) -> "TranscriptBuilder":
        """A manual rename done IN Claude Code (not cagents' own `r`) —
        same "ai-title" record type Claude's auto-title uses, but with
        "customTitle" set instead of "aiTitle"."""
        return self.raw({"type": "ai-title", "customTitle": title, "sessionId": self.session_id})

    def user(
        self, text: str, ts: str = "2026-08-17T10:00:00.000Z", is_meta: bool = False
    ) -> "TranscriptBuilder":
        record = self._base(ts)
        record.update({"type": "user", "message": {"role": "user", "content": text}})
        if is_meta:
            record["isMeta"] = True
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

    def raw_tool_result(
        self, tool_id: str, text: str, ts: str = "2026-08-17T10:00:08.000Z"
    ) -> "TranscriptBuilder":
        record = self._base(ts)
        record.update(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_id, "content": text}
                    ],
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


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Tests must behave identically inside and outside a real tmux session:
    with $TMUX set, Sidecar.enabled() would build a REAL sidecar in any test
    that doesn't inject one, splitting actual panes on the developer's tmux."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("CAGENTS_SIDECAR", raising=False)
    # Warm the memoized version stamp so no test observes its one-time git
    # subprocess (tests that patch subprocess.run globally would catch it).
    from cagents.ctx import version_stamp

    version_stamp()


@pytest.fixture(autouse=True)
def _no_real_agent_status(monkeypatch):
    """`claude agents --json` is a real subprocess call (agent_status.py)
    — never let SessionRegistry.refresh() actually spawn it in tests: it's
    slow (real process spawn) and depends on whatever's genuinely running
    on the machine, and every refresh() in the whole suite calls it once.
    Stubbing the module's _default_runner (not fetch_agent_states itself)
    means a test that explicitly passes SessionRegistry(agents_runner=...)
    still gets its own runner used, since fetch_agent_states only falls
    back to _default_runner when no runner was given."""
    monkeypatch.setattr("cagents.agent_status._default_runner", lambda args: "[]")


@pytest.fixture(autouse=True)
def _no_real_desktop_notifications(monkeypatch):
    """notify_desktop shells out to terminal-notifier / osascript with a
    REAL 10s timeout (notifier.py) — real bug, confirmed live: a slow or
    hung notifier call in one of these real subprocess's paths blocked
    the poll worker that fired it from ever reaching its own later
    refresh_data() call, for the full 10s, even though app.py now
    dispatches the notification onto its own worker instead of calling
    it inline. Never let a test actually invoke it — desktop_notifications
    defaults ON, so any test that causes an ALERT_STATES transition (or
    calls the PR/waiting pollers) would otherwise hit this for real,
    each one adding up to 10 real seconds to the whole suite and, worse,
    being sensitive to whatever terminal-notifier happens to do on the
    machine running the tests."""
    monkeypatch.setattr("cagents.app.notify_desktop", lambda *a, **kw: None)


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


# ---------------------------------------------------------- test doubles --


class FakeTmux:
    """Test double for TmuxClient (v2 multi-socket API)."""

    create_socket = "cagents-sessions"

    def __init__(self):
        from cagents.tmuxctl import TmuxSession  # noqa: F401 (type source)

        self.sessions = []
        self.panes: dict[str, str] = {}
        self.attached_to: list[tuple[str, str]] = []  # (name, socket)
        self.created: list[tuple[str, list[str], str]] = []
        self.sent: list[tuple[str, str, str]] = []  # (name, text, socket)
        self.shell_created: list[tuple[str, str]] = []  # (directory, session_id)
        self.shell_commands: list[tuple[str, str]] = []  # (name, command)
        self.log: list[str] = []

    def available(self) -> bool:
        return True

    def list_sessions(self):
        return self.sessions

    def capture_pane(self, name: str, lines: int = 40, socket: str | None = None) -> str:
        return self.panes.get(name, "")

    def attach(self, name: str, socket: str | None = None) -> int:
        self.attached_to.append((name, socket or self.create_socket))
        return 0

    def send_text(self, name: str, text: str, submit: bool = True, socket: str | None = None):
        self.sent.append((name, text, socket or self.create_socket))

    def new_claude_session(self, directory, claude_args, session_id="", claude_bin=""):
        from pathlib import Path

        from cagents.tmuxctl import TmuxSession

        name = Path(directory).name or "session"
        self.created.append((directory, claude_args, session_id))
        self.sessions.append(
            TmuxSession(
                name=name, created=1e12, activity=1e12, attached=False,
                pane_pid=1, pane_path=directory, socket=self.create_socket,
                cagents_session_id=session_id,
            )
        )
        return name

    def new_shell_session(self, directory, session_id="", extra_env=None):
        from pathlib import Path

        from cagents.tmuxctl import TmuxSession

        name = Path(directory).name or "session"
        self.shell_created.append((directory, session_id))
        self.sessions.append(
            TmuxSession(
                name=name, created=1e12, activity=1e12, attached=False,
                pane_pid=1, pane_path=directory, socket=self.create_socket,
                cagents_session_id=session_id,
            )
        )
        return name

    def send_shell_command(self, name, command, socket=None):
        self.shell_commands.append((name, command))

    def session_statusline_on(self, name, socket=None):
        self.log.append(f"status-on:{name}")

    def session_statusline_off(self, name, socket=None):
        self.log.append(f"status-off:{name}")

    def bind_left_detach(self, tty, socket=None):
        self.log.append(f"bind-left:{tty}")

    def unbind_left_detach(self, socket=None):
        self.log.append("unbind-left")

    def ensure_session_window(self, session_name, window_name, directory, socket=None):
        self.log.append(f"ensure-window:{session_name}:{window_name}:{directory}")

    def ensure_window_view(self, session_name, window_name, socket=None, force_select=False):
        self.log.append(f"ensure-view:{session_name}:{window_name}")
        return f"{session_name}--{window_name}"

    def has_session(self, session_name, socket=None):
        return any(s.name == session_name for s in self.sessions)


class FakeOuterTmux:
    """Records outer-tmux calls; simulates pane creation/liveness."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.panes = ["%0"]  # the cagents rail pane
        self._next = 1

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[0] == "split-window":
            pane = f"%{self._next}"
            self._next += 1
            self.panes.append(pane)
            return pane + "\n"
        if args[0] == "list-panes":
            return "\n".join(self.panes) + "\n"
        return ""


def render_text(content) -> str:
    if hasattr(content, "plain"):
        return content.plain
    import io

    from rich.console import Console

    buffer = io.StringIO()
    console = Console(width=200, file=buffer, force_terminal=False)
    console.print(content)
    return buffer.getvalue()


def widget_text(app, selector: str) -> str:
    return render_text(app.query_one(selector).content)


def select_session(app, session_id: str) -> None:
    """Select a session the way a user would: by moving the list highlight."""
    from cagents.views import SessionList

    session_list = app.query_one(f"#{app.active_view_id}-list", SessionList)
    for i in range(session_list.option_count):
        if session_list.get_option_at_index(i).id == session_id:
            session_list.highlighted = i
            return
    raise AssertionError(f"session {session_id} not in {app.active_view_id} list")


class FakeWorkTmux:
    """Test double for the workspace (tabs) tmux server."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.exists = False
        self.windows: list[str] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        cmd = args[0]
        if cmd == "has-session":
            if not self.exists:
                raise RuntimeError("no session")
            return ""
        if cmd == "new-session":
            self.exists = True
            self.windows = [args[args.index("-n") + 1]]
            return ""
        if cmd == "new-window":
            self.windows.append(args[args.index("-n") + 1])
            return ""
        if cmd == "list-windows":
            return "\n".join(self.windows) + "\n"
        return ""
