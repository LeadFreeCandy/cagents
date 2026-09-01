"""The bare-arrow gate, against rows captured from a real Claude Code
session (v2.1.252) inside the container's session pane.

Each case is what `tmux capture-pane -pe` handed the probe for the row the
cursor was on and its two neighbours, plus the cursor column, plus who the
key belongs to there: SIZE = cagents' pane-size control, CLAUDE = the
session's own arrow key. The long ──── rules are the real capture with
their dash run written as a repeat, and the runs of typed x's are trimmed
to keep the file readable; nothing else is edited.

The rule these pin down was written after the previous one (any dim cell
anywhere on the cursor row) failed four of them -- every failure in the
direction that hands ← back to Claude, which opens ITS agents view over
the session.
"""

from cagents.composer_probe import composer_is_empty

RULE = "\x1b[38;5;244m" + "\u2500" * 165

# label, whose key it is, cursor_x, row above, cursor row, row below
CASES = [
    (
        'empty composer, no placeholder',
        'SIZE',
        2,
        RULE,
        '❯\xa0',
        RULE,
    ),
    (
        'text, cursor at end',
        'CLAUDE',
        13,
        RULE,
        '❯\xa0hello world',
        RULE,
    ),
    (
        'text, cursor at column 2',
        'CLAUDE',
        2,
        RULE,
        '❯\xa0hello world',
        RULE,
    ),
    (
        'text, cursor mid-word',
        'CLAUDE',
        11,
        RULE,
        '❯\xa0hello world',
        RULE,
    ),
    (
        'wrapped text, cursor at start of a wrapped row',
        'CLAUDE',
        2,
        '  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        '  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        RULE,
    ),
    (
        'multi-line, cursor at start of line 2',
        'CLAUDE',
        2,
        '❯\xa0line one',
        '  line two',
        RULE,
    ),
    (
        'multi-line, cursor on line 1',
        'CLAUDE',
        2,
        RULE,
        '❯\xa0line one',
        '  line two',
    ),
    (
        'slash menu open, cursor at column 2',
        'CLAUDE',
        2,
        RULE,
        '❯\xa0/mod',
        RULE,
    ),
    (
        'file picker open, cursor at column 2',
        'CLAUDE',
        2,
        RULE,
        '❯\xa0@src/cag',
        RULE,
    ),
    (
        'bash mode with text, cursor at column 2',
        'CLAUDE',
        2,
        '\x1b[38;5;211m────────────────────────────────────────',
        '\x1b[38;5;211m!\xa0\x1b[39mls -la',
        '\x1b[38;5;211m────────────────────────────────────────',
    ),
    (
        'bash mode, empty',
        'SIZE',
        2,
        '\x1b[38;5;211m────────────────────────────────────────',
        '\x1b[38;5;211m!\xa0\x1b[39m',
        '\x1b[38;5;211m────────────────────────────────────────',
    ),
    (
        'leading-space text, cursor at column 2',
        'CLAUDE',
        2,
        RULE,
        '❯\xa0   indented',
        RULE,
    ),
    (
        '/model dialog (arrows adjust effort)',
        'CLAUDE',
        3,
        '     \x1b[38;5;246m5. \x1b[39mHaiku                  \x1b[38;5;246mHaiku 4.5 · Fastest for quick answers\x1b[39m',
        '   \x1b[38;5;153m❯\x1b[39m \x1b[38;5;246m6. \x1b[38;5;114mOpus\x1b[39m \x1b[38;5;114m✔\x1b[39m                 \x1b[38;5;174mOpus 5\x1b[38;5;246m · Best for everyday, complex tasks\x1b[39m',
        '',
    ),
    (
        'claude agents view',
        'SIZE',
        2,
        '\x1b[2m────────────────────────────────────────',
        '\x1b[38;5;246m❯\x1b[39m \x1b[38;5;246mdescribe a task for a new session\x1b[39m',
        '\x1b[2m────────────────────────────────────────',
    ),
    (
        'empty again after the tour',
        'SIZE',
        2,
        RULE,
        '❯\xa0',
        RULE,
    ),
]


class TestComposerIsEmpty:
    def test_every_captured_state(self):
        wrong = [
            label for label, expect, cx, above, cur, below in CASES
            if composer_is_empty(above, cur, below, cx) != (expect == "SIZE")
        ]
        assert not wrong

    def test_an_empty_composer_with_no_placeholder_is_still_empty(self):
        """The "Try ..." hint stops coming back once you have typed
        anything in a session, leaving a bare prompt. That is the state
        you are in almost always, and the dim-attribute rule read it as
        text -- so ← went to Claude and opened the agents view."""
        assert composer_is_empty(RULE, "\u276f\xa0", RULE, 2)

    def test_a_grey_placeholder_counts_too(self):
        """Not every hint is dim: the agents view and a queued message
        draw theirs in 256-colour grey."""
        cur = "\x1b[38;5;246m\u276f\x1b[39m \x1b[38;5;246mdescribe a task\x1b[39m"
        assert composer_is_empty(RULE, cur, RULE, 2)

    def test_bash_mode_colours_the_prompt_not_your_text(self):
        """Why the rule reads the cell UNDER the cursor rather than asking
        whether anything on the row is styled: `!` mode paints the prompt
        pink and resets before the text."""
        assert composer_is_empty(RULE, "\x1b[38;5;211m!\xa0\x1b[39m", RULE, 2)
        assert not composer_is_empty(RULE, "\x1b[38;5;211m!\xa0\x1b[39mls -la", RULE, 2)

    def test_the_framing_rules_are_required(self):
        """A dialog can park the cursor on styled text too (the /model
        picker, where ←/→ adjust effort). Without the composer's two
        ──── rules around the row, the key is Claude's."""
        cur = "   \x1b[38;5;246m\u276f 6. Opus\x1b[39m"
        assert not composer_is_empty("   1. Default", cur, "", 2)

    def test_past_the_prompt_column_is_always_claude(self):
        """The binding only shells out at column 2, but the probe repeats
        the check rather than trusting its caller."""
        assert not composer_is_empty(RULE, "\u276f\xa0", RULE, 9)
