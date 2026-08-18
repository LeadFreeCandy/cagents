"""The fleet palette: ask Claude to manage cagents' own state, safely.

`:` opens a one-line prompt. The request plus a read-only snapshot of the
session table goes to `claude -p`; Claude answers with a *plan* — a JSON
list of proposed actions on cagents' own store (mark reviewed, set
note/label, untrack) with reasons. Nothing happens until the human
confirms the plan on screen.

Spec guardrails (§5/§10/§11) this deliberately respects:
- never in the core loop: explicitly invoked, runs async, can be ignored;
- can only touch cagents' review state — never Claude's sessions, files,
  or processes (actions are whitelisted here, not by the model);
- fails loudly: a malformed reply is shown as an error, never guessed at.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .format import human_age
from .sessions import Snapshot

# The only things a plan is allowed to do. Anything else is dropped loudly.
ALLOWED_ACTIONS = {
    "mark_reviewed",
    "clear_reviewed",
    "set_label",
    "untrack",
}

PROMPT_TEMPLATE = """\
You are the fleet assistant inside "cagents", a terminal supervisor for Claude Code \
sessions. You manage ONLY cagents' own bookkeeping about sessions (review state, notes, \
labels, tracking). You cannot touch the sessions themselves.

Here is the current session table (JSON):
{table}

The user's request:
{request}

Reply with a single JSON object, no markdown fences, no prose outside it:
{{
  "reply": "<one or two sentences for the user>",
  "actions": [
    {{"action": "<one of: mark_reviewed, clear_reviewed, set_label, untrack>",
      "session_id": "<full session_id from the table>",
      "value": "<text for set_label, omit otherwise>",
      "reason": "<short why>"}}
  ]
}}

Rules: only propose actions the request clearly asks for; if the request is a question, \
answer it in "reply" with an empty actions list; never invent session ids."""


@dataclass
class PlanAction:
    action: str
    session_id: str
    value: str = ""
    reason: str = ""


@dataclass
class Plan:
    reply: str
    actions: list[PlanAction]
    dropped: list[str]  # descriptions of proposed actions that failed validation


class ClaudeRunner(Protocol):
    def run(self, prompt: str) -> str: ...


class CliClaudeRunner:
    """Runs a one-shot `claude -p` in print mode. Slow (seconds), so always
    call from a worker thread."""

    def __init__(self, claude_bin: str = "", timeout: float = 120.0, extra_args: tuple = ()):
        self.claude_bin = claude_bin or shutil.which("claude") or "claude"
        self.timeout = timeout
        self.extra_args = list(extra_args)

    def run(self, prompt: str) -> str:
        proc = subprocess.run(
            [self.claude_bin, "-p", prompt, *self.extra_args, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p failed: {proc.stderr.strip()[:200] or 'unknown error'}")
        return proc.stdout


def fleet_table(snapshot: Snapshot) -> str:
    """The read-only context handed to the model."""
    now = datetime.now(timezone.utc)
    rows = []
    for view in snapshot.views:
        rows.append(
            {
                "session_id": view.session_id,
                "title": view.title,
                "project": view.project_dir,
                "state": view.state.value,
                "state_detail": view.state_detail,
                "last_active": human_age(view.last_activity, now) + " ago",
                "live": view.live,
                "label": view.tracked.label,
            }
        )
    return json.dumps(rows, indent=1)


def build_prompt(snapshot: Snapshot, request: str) -> str:
    return PROMPT_TEMPLATE.format(table=fleet_table(snapshot), request=request)


def _extract_json(text: str) -> dict:
    """Pull the outermost JSON object out of a model reply."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def parse_plan(raw_reply: str, snapshot: Snapshot) -> Plan:
    """Validate the model's reply into a Plan. Invalid actions are dropped
    into `plan.dropped` so the user sees exactly what was refused."""
    data = _extract_json(raw_reply)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    reply = str(data.get("reply", "")).strip()
    known_ids = {view.session_id for view in snapshot.views}
    actions: list[PlanAction] = []
    dropped: list[str] = []
    raw_actions = data.get("actions", [])
    if not isinstance(raw_actions, list):
        raise ValueError("'actions' is not a list")
    for item in raw_actions:
        if not isinstance(item, dict):
            dropped.append(f"not an object: {str(item)[:60]}")
            continue
        action = str(item.get("action", ""))
        session_id = str(item.get("session_id", ""))
        if action not in ALLOWED_ACTIONS:
            dropped.append(f"disallowed action '{action}'")
            continue
        if session_id not in known_ids:
            dropped.append(f"unknown session '{session_id[:13]}…' for {action}")
            continue
        actions.append(
            PlanAction(
                action=action,
                session_id=session_id,
                value=str(item.get("value", "") or ""),
                reason=str(item.get("reason", "") or ""),
            )
        )
    return Plan(reply=reply, actions=actions, dropped=dropped)


def apply_plan(plan: Plan, store, now_iso: str) -> list[str]:
    """Apply a confirmed plan to cagents' store. Returns human-readable
    lines describing what happened."""
    done: list[str] = []
    for act in plan.actions:
        short = act.session_id[:8]
        if act.action == "mark_reviewed":
            store.mark_reviewed(act.session_id, now_iso)
            done.append(f"reviewed {short}")
        elif act.action == "clear_reviewed":
            store.clear_reviewed(act.session_id)
            done.append(f"unreviewed {short}")
        elif act.action == "set_label":
            store.set_label(act.session_id, act.value)
            done.append(f"label on {short}")
        elif act.action == "untrack":
            store.untrack(act.session_id)
            done.append(f"untracked {short}")
    return done
