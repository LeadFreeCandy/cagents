"""Pause & wake: shelve a todo now, get it back automatically.

Three ways back:
- a timer ("2d", "6h", "45m") — wake_at;
- a wake condition in plain English — Claude writes a small check script
  (shown to you before it's saved) that cagents runs periodically; exit 0
  wakes the todo;
- neither — it sleeps until you unpause it.

Auto-pause: open todos with no activity for `auto_pause_days` (settings)
quietly pause themselves instead of nagging forever.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}

SCRIPT_CHECK_INTERVAL = 300.0  # run each wake script at most every 5 min
SCRIPT_TIMEOUT = 30.0


def parse_duration(text: str) -> float | None:
    """'30m' / '4h' / '2d' / '1w' -> seconds, else None."""
    match = _DURATION.match(text)
    if not match:
        return None
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def iso_in(seconds: float, now: float | None = None) -> str:
    base = time.time() if now is None else now
    return datetime.fromtimestamp(base + seconds, tz=timezone.utc).isoformat()


def _iso_to_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


WAKE_SCRIPT_PROMPT = """\
Write a small POSIX shell script that checks whether this condition is true:

    {criteria}

Context: the script runs on macOS, non-interactively, from any directory. The
relevant project directory is {project_dir} (cd there yourself if needed).
Available tools include git, gh (authenticated), curl, jq.

Hard requirements:
- exit 0 if (and only if) the condition is met; exit 1 otherwise
- read-only: it must never modify anything (no pushes, writes, posts)
- finish within a few seconds; no loops or sleeps
- no interactive commands

Reply with ONLY the script body inside one ```sh fenced block, nothing else."""


def build_wake_prompt(criteria: str, project_dir: str) -> str:
    return WAKE_SCRIPT_PROMPT.format(criteria=criteria, project_dir=project_dir or "~")


def extract_script(reply: str) -> str:
    """Pull the fenced script out of the model reply. Raises ValueError if
    there's nothing usable."""
    match = re.search(r"```(?:sh|bash|shell)?\s*\n(.*?)```", reply, re.DOTALL)
    body = (match.group(1) if match else reply).strip()
    if not body or len(body.splitlines()) > 60:
        raise ValueError("no usable script in reply")
    if not body.startswith("#!"):
        body = "#!/bin/sh\n" + body
    return body + "\n"


@dataclass
class WakeReport:
    woken: list[tuple[str, str]] = field(default_factory=list)  # (todo_id, why)
    auto_paused: list[str] = field(default_factory=list)


class WakeEngine:
    """Periodic pass over paused/open todos. All I/O-free decisions are
    injectable so tests control the clock and the script runner."""

    def __init__(self, store: Store, run_script=None):
        self.store = store
        self._run_script = run_script or self._run_real
        self._last_script_check: dict[str, float] = {}

    @staticmethod
    def _run_real(script_path: str) -> bool:
        try:
            proc = subprocess.run(
                ["/bin/sh", script_path], capture_output=True, timeout=SCRIPT_TIMEOUT
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def tick(self, now: float | None = None, last_activity=None) -> WakeReport:
        """last_activity: callable(todo) -> float | None (unix ts of the
        todo's most recent linked-session activity)."""
        now = time.time() if now is None else now
        report = WakeReport()

        for todo in list(self.store.todos.values()):
            if todo.done:
                continue
            if todo.paused:
                self._check_wake(todo, now, report)
            else:
                self._check_auto_pause(todo, now, report, last_activity)
        return report

    def _check_wake(self, todo, now: float, report: WakeReport) -> None:
        if todo.wake_at:
            wake_ts = _iso_to_ts(todo.wake_at)
            if wake_ts is not None and now >= wake_ts:
                self.store.unpause_todo(todo.todo_id)
                report.woken.append((todo.todo_id, "timer elapsed"))
                return
        if todo.wake_script and Path(todo.wake_script).is_file():
            last = self._last_script_check.get(todo.todo_id, 0.0)
            if now - last < SCRIPT_CHECK_INTERVAL:
                return
            self._last_script_check[todo.todo_id] = now
            if self._run_script(todo.wake_script):
                why = todo.wake_criteria or "wake condition met"
                self.store.unpause_todo(todo.todo_id)
                report.woken.append((todo.todo_id, why))

    def _check_auto_pause(self, todo, now: float, report: WakeReport, last_activity) -> None:
        days = self.store.get_setting("auto_pause_days")
        if not isinstance(days, (int, float)) or days <= 0:
            return
        newest: float | None = None
        if last_activity is not None:
            newest = last_activity(todo)
        if newest is None:
            newest = _iso_to_ts(todo.created_at)
        if newest is None:
            return
        if now - newest > days * 86400.0:
            self.store.pause_todo(
                todo.todo_id,
                paused_at=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                wake_criteria=f"auto-paused after {int(days)}d idle",
            )
            report.auto_paused.append(todo.todo_id)
