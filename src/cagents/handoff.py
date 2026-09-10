"""Handoff: close out a conversation by having IT write the spec for its
successor.

Claude writes the spec on a throwaway fork; Codex uses a bounded transcript
excerpt in a separate read-only invocation. The original transcript is never
touched. The spec plus your new prompt becomes the first message of a fresh
session in the chosen provider and model; the old one is marked done (restore
anytime with `d`).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HandoffRequest:
    prompt: str
    provider: str
    model: str = ""  # empty uses the destination provider's configured default


def model_choices(provider: str, codex_dir: Path, observed: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Local suggestions, not a promise of account access. Custom IDs also work.

    Opening a picker must not launch a CLI or fetch models over the network.
    Codex maintains this catalog itself; observed transcript models also cover
    custom providers and installations with no cached catalog.
    """
    import json

    choices = {"": "Provider default"}
    if provider == "claude":
        choices.update({"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"})
    else:
        path = codex_dir / "models_cache.json"
        try:
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            data = json.loads(raw) if len(raw) <= 2 * 1024 * 1024 else {}
            models = data.get("models", []) if isinstance(data, dict) else []
            for model in models if isinstance(models, list) else []:
                if not isinstance(model, dict) or model.get("visibility") == "hide":
                    continue
                slug = model.get("slug")
                if isinstance(slug, str) and slug:
                    choices[slug] = str(model.get("display_name") or slug)
        except (OSError, ValueError):
            pass
    for model in observed:
        if model and not model.startswith("<"):
            choices.setdefault(model, model)
    return [(label, value) for value, label in choices.items()]

HANDOFF_SUMMARY_PROMPT = """\
This conversation is being handed off to a brand-new agent session with none of
your context. Write the handoff spec it will receive as its first message.

Condense everything that matters from this conversation:
- Goal: what we are ultimately building/doing and why
- State: what is DONE and verified vs in-progress vs not started
- Key decisions and their reasons (so they don't get relitigated)
- Files/paths/commands that matter, and any gotchas discovered the hard way
- Immediate next steps

The new session's specific focus will be: {prompt}

Reply with ONLY the spec (no preamble, no meta-commentary). Be dense but complete —
the new session knows nothing except what you write."""


def summary_prompt(prompt: str) -> str:
    return HANDOFF_SUMMARY_PROMPT.format(prompt=prompt)


def first_message(spec: str, prompt: str) -> str:
    return (
        "You are taking over from a previous session. Its handoff spec:\n\n"
        f"{spec.strip()}\n\n"
        f"Your task: {prompt}"
    )


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
