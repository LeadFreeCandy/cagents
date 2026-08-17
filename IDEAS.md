# Ideas — organic extensions

Brainstorm of directions that extend cagents *with the grain* of the spec: Claude owns
execution, cagents owns visibility and review state. Every idea below is judged against
spec §11 (does it slow the core loop / duplicate Claude / fail loudly). Items marked
**[prototyped]** exist in the `cagents-next` experimental copy, not in this version.

The organizing principle that emerged: **"Claude writes it, cagents shows it."** Claude Code
keeps enriching its own transcript records (`pr-link`, `frame-link`, agent counts, turn
durations...). cagents should grow by *reading more of what Claude already says* — never by
asking Claude to say things for cagents' benefit. That's how it grows elegantly as Claude
gains features, with zero new coupling.

## A. Visibility — more signal from data Claude already writes

- **Peek mode** **[prototyped]** — `space` opens a full-screen, scrollable read of the
  transcript (much deeper than the side preview) without attaching. Review a finished
  session end-to-end, press `r` right there, never touch the live CLI. This strengthens
  core-loop step 3 (see without attaching) and makes "needs review → done" honest: you
  actually looked.
- **Files touched** **[prototyped]** — derive the list of files a session edited from its
  own `Edit`/`Write` tool calls and show it in the detail pane. When a session says *needs
  review*, the first question is always "what did it change?" — this answers it without
  attaching, from data already in the transcript.
- **Links Claude records** **[prototyped]** — transcripts already carry `pr-link` (PRs the
  session opened) and `frame-link` (artifacts) records. Surface them as badges; `o` opens
  the newest in the browser. Zero inference; pure display. New link-ish record types should
  land as badges automatically (registry pattern, one line per new type).
- **Background agent awareness** **[prototyped]** — Claude's `system` records report
  `pendingBackgroundAgentCount`. Show `⑂ N` when a session has agents in flight: "working"
  becomes "working, wide".
- **Attention bell** — optional macOS notification (`osascript`) when any session
  *transitions into* needs-input/needs-review while cagents is running. The entire point of
  the tool is knowing when you're needed; today you must be looking at it. Must be a
  transition-edge notification (never repeated), off by default, and never block refresh.
- **Turn rhythm sparkline** — a tiny braille sparkline of transcript write activity for the
  selected session (last ~10 min). Communicates "cranking / stuck / idle" preattentively,
  cheaper than reading. Derived from file mtimes we already track, so it's free.
- **Waiting-on detail** — when a live pane shows a prompt, lift the *question text* itself
  (first line) into the queue row: `◉ needs you — "Overwrite schema.sql?"`. Display-only
  (state detection stays independent), so a CLI redesign degrades it gracefully to the
  generic label.

## B. Flow — inbox-zero mechanics

- **`]` next-attention** — jump straight to the next session that needs a human, from any
  view. Pairs with the queue ordering that already exists; makes triage muscle-memory.
- **Soft archive** — `z` hides done/stopped sessions from the default views (store keeps
  them; `Z` toggles visibility). Keeps the daily list tight without deleting review history.
  This is also the natural substrate for spec §10's "recommend stale sessions for archival".
- **Fuzzy jump** — `/` filter-as-you-type across title/project/note/label of tracked
  sessions. Matters once tracked count crosses ~15.
- **Lineage** (spec §9 explicitly reserves room for this) — `S` on a session = "supersede":
  starts a fresh session in the same directory and records parent → child in the store.
  Grouped view renders `↳` children under parents. Later enables "this session was
  superseded, archive it?" suggestions that are *facts*, not guesses.
- **Preview scroll courtesy** — pause the auto-follow when you scroll up in the preview;
  resume on `G`/bottom. (Known wart, listed in OPEN_QUESTIONS.)

## C. Growing with Claude — the extension seams

- **Hooks as a push channel (the big one)** — Claude Code hooks (`Notification`, `Stop`,
  permission events) can run arbitrary commands. A `cagents emit` subcommand appended to the
  user's hook config would let *Claude itself* tell cagents "turn ended" / "permission
  needed for Bash: …" the moment it happens — replacing the pane-marker heuristics with
  facts, event-driven instead of polled. Crucially the architecture stays layered: hooks
  present → exact events; hooks absent → today's heuristics. cagents gets *more* correct as
  Claude exposes more, and never breaks when it doesn't. This directly retires the top item
  in OPEN_QUESTIONS.
- **Record-type registry** — the parser already skips unknown record types safely. Formalize
  a tiny table (record type → badge/metadata extractor) so each new thing Claude starts
  writing is a 3-line addition, not a parser change. `pr-link` and `frame-link` prove the
  pattern.
- **Titles, summaries, naming** — wherever Claude's own session naming improves
  (`ai-title` → something richer), cagents inherits it for free because it never stores its
  own copy. Keep it that way.

## D. Meta — Claude managing the fleet (spec §10, done safely)

- **Fleet command palette** **[prototyped]** — `:` opens a command line. Plain commands work
  offline (`:archive done`, `:note …`). But a natural-language request ("mark everything in
  dealpilot reviewed, it's all merged") is handed to `claude -p` with a *read-only snapshot*
  of the session table, and Claude returns a **structured plan** — a list of proposed
  actions on cagents' own store (review/label/note/untrack), each with a reason. cagents
  renders the plan as a confirmation dialog; nothing executes until you say yes. The
  boundaries that make this spec-safe:
    - it can only touch *cagents' state*, never Claude's sessions;
    - it's explicitly invoked, clearly labeled, and async — the core loop never waits on it;
    - the model's output is a plan for a human, not an action.
- **The meta-session** — `M` opens (or creates, first time) a dedicated Claude session *in
  the cagents repo itself*, auto-tracked and labeled `cagents dev`. You manage and extend
  cagents from inside cagents; the supervisor is one keystroke from its own maintainer.
  Costs nothing to build — it's just a pinned tracked session — and it's the honest version
  of "Claude manages this setup": Claude works on cagents the way it works on anything, in a
  real session, visible in the list like everything else.
- **Morning digest (CLI, not TUI)** — `cagents digest` runs offline from the app: feeds the
  last 24h of tracked-session tails to `claude -p` and prints a short standup ("dealpilot
  finished the pricing work overnight — needs review; sts2rl is blocked on a permission").
  Batch, explicitly invoked, zero presence in the interactive loop.

## Judged and rejected (for now)

- **Chat-with-a-session inside cagents** — spec §5 says no, and it's right; that's what
  attach is for.
- **Auto-generated per-session summaries in the list** — latency + cost in the browse path;
  the real transcript tail is better information anyway.
- **Watching all Claude processes automatically** — breaks the "only what you brought in"
  contract (spec §3). The track-picker is the deliberate gate.
- **Cross-machine fleet view** — genuinely attractive with the tmux socket design (a phone
  over SSH already works via `cs`), but it's a new failure surface in the core loop. Revisit
  if/when it hurts.
