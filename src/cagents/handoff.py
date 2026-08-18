"""Handoff: close out a conversation by having IT write the spec for its
successor.

The summary turn runs on a throwaway *fork* of the old session (print
mode), so the original transcript is never touched. The spec plus your
new prompt becomes the first message of a fresh session; the old one is
marked done (restore anytime with `r`).
"""

from __future__ import annotations

HANDOFF_SUMMARY_PROMPT = """\
This conversation is being handed off to a brand-new Claude session with none of
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
