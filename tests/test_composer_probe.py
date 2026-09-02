"""The bare-arrow gate, against the bottom of the session pane captured
from a real Claude Code session (v2.1.258) running inside a real cagents
container.

Each case is what `tmux capture-pane -pe` hands the probe, plus who the
key belongs to there: SIZE = cagents' pane-size control, CLAUDE = the
session's own arrow key. Long ──── rules are written as a repeat and long
runs of typed x's are trimmed; nothing else is edited.

The rule these pin down replaced two earlier ones, both of which failed in
the direction that hands ← to Claude — which opens ITS agents view over
the session:

  * "any dim cell on the cursor row" missed the empty composer you are in
    almost always (once you have typed in a session the "Try ..." hint
    stops coming back) and the grey placeholders (queued message, agents
    view).
  * "the row above and below the cursor are pure ──── rules" failed at
    random, roughly one press in ten, because Claude writes transient
    chips into the top rule — see test_a_chip_in_the_divider.
"""

from cagents.composer_probe import composer_is_empty, is_rule

RULE = "\x1b[38;5;244m" + "\u2500" * 165

# label, whose key it is, the bottom of the pane top-down
CASES = [
    (
        'empty, placeholder',
        'SIZE',
        [
            RULE,
            '\x1b[39m❯\xa0\x1b[2mTry "edit test_flows.py to..."\x1b[0m',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle) · ← 2 agents\x1b[39m',
        ],
    ),
    (
        'text, cursor at end',
        'CLAUDE',
        [
            RULE,
            '\x1b[39m❯\xa0hello world',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m',
        ],
    ),
    (
        'text, cursor at column 2',
        'CLAUDE',
        [
            RULE,
            '\x1b[39m❯\xa0hello world',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m',
        ],
    ),
    (
        'wrapped text',
        'CLAUDE',
        [
            RULE,
            '\x1b[39m❯\xa0xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            '  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            '  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m',
        ],
    ),
    (
        'multi-line',
        'CLAUDE',
        [
            '                                                                                                                                                        \x1b[38;5;246mctrl+g to edit in Vim\x1b[39m',
            RULE,
            '\x1b[39m❯\xa0line one',
            '  line two',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m',
        ],
    ),
    (
        'slash menu open',
        'CLAUDE',
        [
            '  \x1b[38;5;153m/\x1b[1mmod\x1b[0m\x1b[38;5;153mel                        Set the AI \x1b[1mmod\x1b[0m\x1b[38;5;153mel for Claude Code (currently Opus 5 (1M context))\x1b[39m',
            '  \x1b[38;5;246m/claude-api                   Reference for the Claude API / Anthropic SDK — \x1b[1m\x1b[39mmod\x1b[0m\x1b[38;5;246mel ids, pricing, params, streaming, tool use, MCP, agents, caching, token counting, model\x1b[39m',
            '                                \x1b[38;5;246mmigration. TRIGGER — read BEFORE opening the target file; don\'t skip because it "looks like a one-liner" — whenever: the prompt names Claude…\x1b[39m',
            '  \x1b[38;5;246m/auto-\x1b[1m\x1b[39mmod\x1b[0m\x1b[38;5;246me-setup              Teach auto \x1b[1m\x1b[39mmod\x1b[0m\x1b[38;5;246me about your environment, plus optional rule tweaks\x1b[39m',
            RULE,
            '\x1b[39m❯\xa0/mod',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m',
        ],
    ),
    (
        'file picker open',
        'CLAUDE',
        [
            '  \x1b[38;5;246m+ src/cagents/app.py\x1b[39m',
            '  \x1b[38;5;246m+ src/cagents/ctx.py\x1b[39m',
            '  \x1b[38;5;246m+ src/cagents/jira.py\x1b[39m',
            '  \x1b[38;5;246m+ src/cagents/sessions.py\x1b[39m',
            RULE,
            '\x1b[39m❯\xa0@src/cag',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ rl - │ done -%\x1b[0m                                                                                                                           \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m',
        ],
    ),
    (
        'bash mode with text',
        'CLAUDE',
        [
            '                                                                                                                                                 \x1b[38;5;246mCtrl+Y to paste deleted text\x1b[39m',
            '\x1b[38;5;211m───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────',
            '!\xa0\x1b[39mls -la',
            '\x1b[38;5;211m───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────',
            '\x1b[39m  \x1b[38;5;211m! for shell mode\x1b[39m                                                                                                                                                        \x1b[38;5;114m/rc\x1b[39m',
        ],
    ),
    (
        'bash mode, empty',
        'SIZE',
        [
            '\x1b[38;5;211m───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────',
            '!\xa0\x1b[2m\x1b[39mTry "edit test_flows.py to..."\x1b[0m',
            '\x1b[38;5;211m───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────',
            '\x1b[39m  \x1b[38;5;211m! for shell mode\x1b[39m                                                                                                                                                        \x1b[38;5;114m/rc\x1b[39m',
        ],
    ),
    (
        'mode cycled, empty (no placeholder)',
        'SIZE',
        [
            RULE,
            '\x1b[39m❯\xa0\x1b[2mTry "edit test_flows.py to..."\x1b[0m',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ 5h 3%/7d 1% │ done -%\x1b[0m                                                                                                                    \x1b[38;5;114m/rc\x1b[39m',
            '  \x1b[38;5;246m⏸ manual mode on · ← 2 agents\x1b[39m',
        ],
    ),
    (
        '/model dialog',
        'CLAUDE',
        [
            '     \x1b[38;5;246m3. \x1b[39mFable                  \x1b[38;5;174mFable 5.1\x1b[38;5;246m · Most capable for your hardest and longest-running tasks\x1b[39m',
            '     \x1b[38;5;246m4. \x1b[39mSonnet                 \x1b[38;5;246mSonnet 5 · Efficient for routine tasks\x1b[39m',
            '     \x1b[38;5;246m5. \x1b[39mHaiku                  \x1b[38;5;246mHaiku 4.5 · Fastest for quick answers\x1b[39m',
            '',
            '   \x1b[38;5;174m●\x1b[38;5;246m High effort (default)\x1b[38;5;239m ←/→ to adjust\x1b[39m',
            '',
            '   \x1b[38;5;246mUse \x1b[1m/fast\x1b[0m\x1b[38;5;246m to turn on Fast mode (Opus 5).\x1b[39m',
            '',
            '   \x1b[38;5;246mEnter to set as default · s to use this session only · Esc to cancel\x1b[39m',
        ],
    ),
    (
        'empty, no placeholder',
        'SIZE',
        [
            '                                                                                                                                                             \x1b[38;5;246m● high · /effort\x1b[39m',
            RULE,
            '\x1b[39m❯\xa0',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ 5h 3%/7d 1% │ done -%\x1b[0m                                                                                                                    \x1b[38;5;114m\x1b]8;id=tmux1;https://claude.ai/code/session_01V3GWuqFPwFZcce7quzkDs8?from=cli\x1b\\/rc\x1b[39m\x1b]8;;\x1b\\',
            '  \x1b[38;5;73m⏸ plan mode on\x1b[38;5;246m (shift+tab to cycle) · ← 2 agents\x1b[39m',
        ],
    ),
    (
        'claude agents view',
        'SIZE',
        [
            '\x1b[2m\x1b[39m───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────',
            '\x1b[0m\x1b[38;5;246m❯\x1b[39m \x1b[38;5;246mdescribe a task for a new session\x1b[39m',
            '\x1b[2m───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────',
            '\x1b[0m  \x1b[38;5;220m⏵⏵ auto mode\x1b[38;5;246m · enter to return · space to reply · ctrl+x to delete · ? for shortcuts\x1b[39m',
        ],
    ),
    (
        'back from agents view',
        'SIZE',
        [
            '                                                                                                                                                             \x1b[38;5;246m● high · /effort\x1b[39m',
            RULE,
            '\x1b[39m❯\xa0',
            RULE,
            '\x1b[39m  \x1b[2m\x1b[38;5;246mOpus 5 (1M context) │ ctx -% │ 5h 3%/7d 1% │ done -%\x1b[0m                                                                                                                    \x1b[38;5;114m\x1b]8;id=tmux1;https://claude.ai/code/session_01V3GWuqFPwFZcce7quzkDs8?from=cli\x1b\\/rc\x1b[39m\x1b]8;;\x1b\\',
            '  \x1b[38;5;73m⏸ plan mode on\x1b[38;5;246m (shift+tab to cycle) · ← 2 agents\x1b[39m',
        ],
    ),
]


class TestComposerIsEmpty:
    def test_every_captured_state(self):
        wrong = [label for label, expect, rows in CASES
                 if composer_is_empty(rows) != (expect == "SIZE")]
        assert not wrong

    def test_a_chip_in_the_divider(self):
        """The bug this rule exists for: Claude draws a background agent's
        title into the composer's top rule, and a check that wanted a pure
        run of dashes there handed ← to Claude — reproduced live at about
        one press in ten, which is exactly what "sometimes" looked like.
        (Row reconstructed from the capture logged at the failure.)"""
        chip = "\x1b[38;5;244m" + "\u2500" * 155 + " Left arrow logic \u2500"
        assert is_rule(chip)
        assert composer_is_empty([chip, "\u276f\xa0", RULE, "  ready"])

    def test_a_screen_with_no_composer_is_claudes(self):
        """A framed dialog (the /model picker, where ←/→ adjust effort;
        Rewind; a permission prompt) must never read as an empty
        composer."""
        rewind = ["\u2594" * 80, "   Rewind", "", "   Nothing to rewind to yet.",
                  "", "   Esc to cancel"]
        assert not composer_is_empty(rewind)
        assert not composer_is_empty([RULE, "   \u276f 6. Opus", RULE])

    def test_the_placeholder_may_be_dim_or_grey_or_absent(self):
        """Three empty composers Claude actually draws."""
        for cur in ("\x1b[39m\u276f\xa0\x1b[2mTry \"fix lint\"\x1b[0m",   # dim hint
                    "\x1b[38;5;246m\u276f\x1b[39m \x1b[38;5;246mdescribe a task\x1b[39m",  # grey
                    "\x1b[39m\u276f\xa0"):                                  # bare prompt
            assert composer_is_empty([RULE, cur, RULE, "  footer"]), cur

    def test_typed_text_is_never_empty_however_it_is_reached(self):
        for cur in ("\u276f\xa0hello world",            # plain
                    "  line two",                      # a continuation row
                    "\x1b[38;5;211m!\xa0\x1b[39mls -la"):  # bash mode: pink prompt
            assert not composer_is_empty([RULE, cur, RULE, "  footer"]), cur
