# Open implementation questions

Honest notes on where the current implementation makes judgment calls, and what might need
revisiting. Read alongside the product spec (§ references are to it).

## State detection is heuristic at the edges (§6)

*Needs input vs working* for a session with an unanswered tool call is decided by pane
markers first ("esc to interrupt" → working; "Do you want…" → needs input), then by
transcript write recency (fresh writes → working). Two known gaps:

- A long-running quiet tool in a session whose pane markers change in a future Claude CLI
  redesign could misreport as *needs input*. The marker strings live in `tmuxctl.py`
  (`_PROMPT_MARKERS`, `_WORKING_MARKERS`) and are cheap to update.
- A session at the idle prompt where the human typed half a message shows *needs input*
  ("at the prompt") only if the last transcript record is a user message; if Claude had
  replied, it shows *needs review*. That seems right, but real usage may say otherwise.

## tmux ↔ session mapping for wrapper-started sessions (§4.4)

Sessions started by `cagents` map exactly (CAGENTS_SESSION_ID env var). Sessions started by
the `claude-tmux` wrapper are matched by pane cwd + transcript mtime vs tmux creation time.
Two Claude sessions started in the same directory at nearly the same time outside cagents
could, in principle, swap identities in the list until one of them writes. Options if this
bites: have the wrapper also set the env var (it would need the session id, which claude
doesn't expose pre-launch — it could generate one and pass `--session-id`), or accept it.

## Attach nesting

Attaching from inside another tmux (e.g. running `cagents` itself inside tmux) works by
unsetting `$TMUX` for the attach — deliberate nesting. The double prefix-key (`ctrl-b ctrl-b d`
to detach the inner session) is standard tmux nesting friction. If cagents itself ends up
living in a tmux pane day-to-day, a `switch-client`-based flow on the same socket would feel
nicer, but only helps when cagents runs on the *claude* socket.

## Preview follows the tail

The preview pane auto-scrolls to the newest message on every refresh. Scrolling up to read
history therefore fights the 2s refresh while the session is being written to. If that's
annoying in practice: pause auto-scroll while the user has scrolled away from the bottom.

## Permission-prompt detail

When a live pane shows a prompt, the state detail says which tool is waiting
(`permission: Bash`) based on the newest unanswered tool call. AskUserQuestion-style
prompts (not tool permissions) show as generic "waiting on you". Could parse the pane text
for the question itself — deliberately not done yet (fragile).

## Stopped vs failed (§6)

The spec names "Stopped / Failed"; the implementation has only *stopped* (transcript ends
mid-turn with no live process). Claude Code doesn't persist an explicit failure record to
the transcript; distinguishing crash from quit would require evidence we don't have.

## Store growth

Untracked-but-reviewed state: untracking a session deletes its review state. Re-tracking it
later starts fresh. Fine for now; worth knowing.
