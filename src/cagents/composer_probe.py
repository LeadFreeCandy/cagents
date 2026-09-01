#!/usr/bin/env python3
"""Does a bare arrow in the session pane belong to the size control or to
Claude? Read the composer and decide.

This file is both a module (so the rule can be unit-tested against real
captures) and the script the tmux binding runs: `_write_composer_probe`
copies it next to the other shims with a `#!<interpreter>` line on top, so
it must stay free of intra-package imports.

argv: <key> <pane_id> <cursor_y> <cursor_x> <zoomed_flag>. The exit status
is ignored; the script performs the action itself. Anything unexpected --
a failed capture, a screen that doesn't look like the composer -- sends
the key to Claude: losing the size control on one press is harmless,
swallowing a keystroke mid-sentence is not.

WHAT SEPARATES EMPTY FROM TYPED. Not the cursor column: column 2 means
"start of a line", and every wrapped or multi-line row starts there with
text present. Not a dim attribute anywhere on the row either -- that was
the previous rule and it misses the two empty composers you actually meet
(once you have typed anything in a session the "Try ..." hint stops coming
back, leaving a bare `❯`; a queued message and Claude's own agents view
draw their hints in 256-colour grey, not dim).

What holds across all of them is the cell UNDER the cursor: an empty
composer has either nothing there or a styled placeholder, while typed
text is always drawn in the terminal's default foreground. Bash mode is
the case that rules out "is anything on this row styled" -- it colours the
`!` prompt pink and resets before your text.

The two horizontal rules above and below are required as well: they frame
the composer, and their absence is what keeps a permission prompt or the
/model picker (where ←/→ adjust effort) from being mistaken for it.
"""

import re
import subprocess
import sys
import unicodedata

SGR = re.compile(r"\033\[([0-9;]*)m")

# An empty composer parks the cursor right after the two-cell prompt
# (`❯ ` / `! `). Anything further right is text, so the binding never even
# calls us there.
EMPTY_COMPOSER_CURSOR_X = 2


def cells(raw):
    """[(char, styled)] indexed by display column, styled meaning the cell
    carries a foreground colour or dim/italic — anything Claude paints and
    the user's own typing doesn't."""
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
        char = raw[i]
        if char == "\n":
            break
        styled = fg is not None or bool(attrs)
        out.append((char, styled))
        if unicodedata.east_asian_width(char) in ("W", "F"):
            out.append(("", styled))  # a wide glyph owns two columns
        i += 1
    return out


def is_rule(raw):
    """One of the ──── lines that frame the composer."""
    text = SGR.sub("", raw).strip()
    return len(text) > 10 and set(text) <= {"─"}


def composer_is_empty(above, cur, below, cursor_x):
    if cursor_x > EMPTY_COMPOSER_CURSOR_X:
        return False
    if not (is_rule(above) and is_rule(below)):
        return False
    row = cells(cur)
    if cursor_x >= len(row):
        return True  # nothing drawn under or after the cursor at all
    if not "".join(char for char, _ in row[cursor_x:]).strip():
        return True
    return row[cursor_x][1]


def capture(pane, top, bottom):
    out = subprocess.run(
        ["tmux", "capture-pane", "-pe", "-t", pane, "-S", str(top), "-E", str(bottom)],
        capture_output=True, text=True, timeout=3,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    # capture-pane drops trailing blank cells, so short rows come back
    # short and a blank row comes back empty; pad to the range asked for.
    lines = out.stdout.split("\n")
    return (lines + [""] * 3)[: bottom - top + 1]


def tmux(*args):
    subprocess.run(["tmux", *args], capture_output=True, timeout=3)


def main(argv):
    key, pane, cursor_y, cursor_x, zoomed = argv[1], argv[2], int(argv[3]), int(argv[4]), argv[5]
    try:
        above, cur, below = capture(pane, cursor_y - 1, cursor_y + 1)
        ours = composer_is_empty(above, cur, below, cursor_x)
    except Exception:
        ours = False
    if not ours:
        tmux("send-keys", "-t", pane, key)
    elif key == "Left" and zoomed == "1":       # HIDDEN -> SMALL
        tmux("resize-pane", "-Z", "-t", ":.1")
        tmux("select-pane", "-t", ":.1")
    elif key == "Left":                          # SMALL  -> WIDE
        tmux("select-pane", "-t", ":.0")
    elif key == "Right":                         # SMALL  -> HIDDEN
        tmux("resize-pane", "-Z", "-t", ":.1")
    else:
        tmux("send-keys", "-t", pane, key)


if __name__ == "__main__":
    main(sys.argv)
