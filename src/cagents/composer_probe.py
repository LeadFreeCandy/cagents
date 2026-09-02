#!/usr/bin/env python3
"""Does a bare arrow in the session pane belong to the size control or to
Claude? Read the composer and decide.

This file is both a module (so the rule can be unit-tested against real
captures) and the script the tmux binding runs: `_write_composer_probe`
copies it next to the other shims with a `#!<interpreter>` line on top, so
it must stay free of intra-package imports.

argv: <key> <pane_id> <zoomed_flag>, where key is whatever tmux is bound
to (Left, C-Right, ...). The exit status is ignored; the script performs
the action itself.

WHAT THIS HAS TO GET RIGHT. Claude Code binds ← on an empty composer to
its own agents view, so a miss is not a missing feature -- it drops the
user into another screen entirely. And taking the key while they are
typing stops their cursor. Both directions are loud, so the rule reads the
composer itself rather than anything about the cursor:

  * The composer is the block between the last two ──── rules at the
    bottom of the pane. Rules are matched loosely (mostly dashes), because
    Claude draws transient chips into the top one -- a background agent's
    title, for instance. Demanding a pure run of dashes there is what made
    an earlier version hand ← to Claude every so often, seemingly at
    random: it only failed while a chip happened to be on screen.
  * Inside it, TYPED TEXT IS UNSTYLED and every placeholder is styled --
    dim for "Try ...", 256-colour grey for "Press up to edit queued
    messages" and for the agents view's own prompt. So the composer is
    empty when no row holds an unstyled, non-blank character after the
    prompt. (A composer holding nothing but a highlighted @mention reads
    as empty by this rule. That costs a trip to the rail, with the text
    still there; ⌥→ or → walks straight back.)
  * Nothing here depends on cursor_x/cursor_y beyond the binding's own
    fast path, so a redraw between tmux evaluating the format and this
    script capturing can no longer flip the answer.

Anything unexpected -- a failed capture, a screen with no composer in it
(a permission prompt, the /model picker, where ←/→ adjust effort) -- sends
the key to Claude.
"""

import re
import subprocess
import sys

SGR = re.compile(r"\033\[([0-9;]*)m")

# An empty composer parks the cursor right after the two-cell prompt
# (`❯ ` / `! `). Anything further right is text, so the binding never even
# calls us there.
EMPTY_COMPOSER_CURSOR_X = 2
PROMPT_WIDTH = 2
PROMPTS = ("❯", ">", "!", "#")
ROWS_TO_READ = 16  # enough for a composer several lines tall plus its footer


def styled_cells(raw):
    """[(char, styled)] for one captured row. `styled` means the cell
    carries a foreground colour or dim/italic — anything Claude paints,
    and nothing the user's own typing has."""
    out, i, fg, attrs = [], 0, None, set()
    while i < len(raw):
        match = SGR.match(raw, i)
        if match:
            params = [p for p in match.group(1).split(";") if p] or ["0"]
            k = 0
            while k < len(params):
                p = params[k]
                if p == "0":
                    fg, attrs = None, set()
                elif p in ("2", "3"):
                    attrs.add(p)
                elif p in ("22", "23"):
                    attrs.discard("2" if p == "22" else "3")
                elif p == "39":
                    fg = None
                elif p == "38" and k + 1 < len(params):
                    width = 3 if params[k + 1] == "5" else 5
                    fg, k = ";".join(params[k : k + width]), k + width - 1
                elif p.isdigit() and (30 <= int(p) <= 37 or 90 <= int(p) <= 97):
                    fg = p
                k += 1
            i = match.end()
            continue
        if raw[i] == "\n":
            break
        out.append((raw[i], fg is not None or bool(attrs)))
        i += 1
    return out


def is_rule(raw):
    """One of the lines framing the composer. Loose on purpose: Claude
    writes into the top rule (an agent's title, right-aligned), so this
    asks for a long run of dashes, not a row made of nothing else."""
    text = SGR.sub("", raw).strip()
    dashes = text.count("─")
    solid = len(text.replace(" ", ""))
    return dashes >= 20 and solid and dashes / solid >= 0.6


def composer_rows(rows):
    """The rows between the last two rules, or None if the bottom of the
    screen doesn't look like a composer at all."""
    marks = [i for i, row in enumerate(rows) if is_rule(row)]
    if len(marks) < 2:
        return None
    bottom, top = marks[-1], marks[-2]
    block = rows[top + 1 : bottom]
    if not block:
        return None
    head = SGR.sub("", block[0])
    if not head[:1] in PROMPTS:
        return None  # a framed dialog, not the composer
    return block


def composer_is_empty(rows):
    """rows: the bottom of the session pane, `capture-pane -pe`, top-down."""
    block = composer_rows(rows)
    if block is None:
        return False
    for line in block:
        for char, styled in styled_cells(line)[PROMPT_WIDTH:]:
            if not styled and char.strip():
                return False
    return True


def capture(pane, rows=ROWS_TO_READ):
    """The bottom `rows` lines of the visible pane, attributes kept."""
    out = subprocess.run(
        ["tmux", "capture-pane", "-pe", "-t", pane, "-S", "0", "-E", "-"],
        capture_output=True, text=True, timeout=3,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    lines = out.stdout.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # capture-pane ends with a newline, not a blank row
    return lines[-rows:]


def tmux(*args):
    subprocess.run(["tmux", *args], capture_output=True, timeout=3)


def decide(pane, attempts=3):
    """Two reads that agree, or a third to break the tie: a capture taken
    mid-redraw can show a composer that isn't finished being drawn."""
    votes = []
    for _ in range(attempts):
        votes.append(composer_is_empty(capture(pane)))
        if len(votes) >= 2 and votes[-1] == votes[-2]:
            return votes[-1]
    return max(set(votes), key=votes.count)


def main(argv):
    key, pane, zoomed = argv[1], argv[2], argv[3]
    arrow = key.rsplit("-", 1)[-1]  # "Left" / "C-Left" -> Left
    try:
        ours = decide(pane)
    except Exception:
        ours = False
    if not ours:
        tmux("send-keys", "-t", pane, key)
    elif arrow == "Left" and zoomed == "1":     # HIDDEN -> SMALL
        tmux("resize-pane", "-Z", "-t", ":.1")
        tmux("select-pane", "-t", ":.1")
    elif arrow == "Left":                        # SMALL  -> WIDE
        tmux("select-pane", "-t", ":.0")
    elif arrow == "Right":                       # SMALL  -> HIDDEN
        tmux("resize-pane", "-Z", "-t", ":.1")
    else:
        tmux("send-keys", "-t", pane, key)


if __name__ == "__main__":
    main(sys.argv)
