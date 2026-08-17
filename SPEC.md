# cagents — Product Specification

This supersedes the original spec doc for day-to-day direction. It's deliberately written at the
level of *what cagents must do and why*, not *how it's built* — implementation is free to change as
long as it keeps serving what's written here. See `README.md` for what's actually implemented today
and `OPEN_QUESTIONS.md` for open implementation questions.

## 1. Purpose

`cagents` is a lightweight terminal supervisor for Claude Code sessions you're actively using for
real work. It is not a new agent runtime, not a task orchestrator, and not a replacement for the
Claude CLI. Claude Code remains fully responsible for running sessions, managing worktrees, and
persisting conversations. `cagents` adds a better view over that, and a small amount of human review
state Claude has no reason to track itself.

The mental model:

> **Claude owns execution. `cagents` owns visibility and, where useful, human review state.**

## 2. Core principles

- **Claude is the runtime, always.** If a feature starts duplicating something Claude Code already
  does — running a model, storing a transcript, managing a worktree — that's a sign the feature is
  wrong, not that `cagents` needs to get better at it.
- **The native Claude CLI is never more than one keystroke away.** Attaching to a session must
  always hand off to the real, live thing — never a `cagents`-drawn substitute.
- **Human review state is distinct from Claude's state.** Claude finishing a task doesn't mean a
  human has looked at it. `cagents` is allowed to track that distinction; nothing else.
- **Persist the minimum.** Whatever `cagents` remembers about a session should be only what Claude
  itself has no way to represent (review status, a note, lightweight lineage). Never a second copy
  of Claude's own session data.
- **Correctness and speed of the core loop outrank every feature.** A feature that makes opening,
  browsing, or attaching to a session slower, flakier, or less predictable does not ship, no matter
  how useful it is on its own — see §6 and §9.

## 3. Scope: what `cagents` tracks

`cagents` only ever shows sessions you deliberately brought into it — not every Claude process
running on the machine. This keeps the list small, predictable, and yours: opening `cagents` should
never surprise you with something you didn't ask it to track.

## 4. The core loop (MVP — current priority, in order)

Everything else is secondary to making this loop feel instant and trustworthy:

1. **Open `cagents` and see your sessions immediately.** No visible delay waiting on anything beyond
   the machine's own current state — never a network call, a model call, or anything else with
   unpredictable latency between launching and having a usable screen.
2. **Move between sessions smoothly.** Navigating the list must never flicker, never silently lose
   your place, never make an action you just took look like it didn't happen.
3. **See what's actually happening in a session without attaching to it.** A live, real preview of
   the conversation itself — not a generated summary, not stale metadata — refreshed automatically
   as the session progresses.
4. **Press Enter and be in the real session, every time.** Selecting a session and attaching must be
   completely reliable. If it doesn't work, nothing else about `cagents` matters.

Any change that risks any of these four is the wrong change until they're solid.

## 5. Explicit non-goals, for now

These aren't rejected ideas — they're sequenced behind §4 on purpose, because each one introduces
latency, cost, or a new way for the core loop to fail:

- **Any AI-assisted feature** — natural-language commands, generated summaries of a session,
  semantic search over history. Real API calls have real, sometimes multi-second latency and real
  cost; none of that is acceptable anywhere near opening the app, browsing, or attaching.
- **A chat interface inside `cagents`.** Attaching means handing off to the real Claude CLI, full
  stop — never a `cagents`-native way to converse with a session.
- **Reimplementing anything Claude Code already owns** — its own session store, worktree management,
  or conversation transcripts. `cagents` reads these; it never becomes a second copy of them.

Revisit these only once §4 is proven solid in real day-to-day use — not before.

## 6. Session lifecycle

A session's state as `cagents` shows it should always answer two questions at a glance: *is Claude
doing something right now*, and *does this need a human*. Broadly:

- **Working** — Claude is actively doing something.
- **Needs input / needs permission** — Claude is blocked on the human.
- **Needs review** — Claude finished, but no human has looked at the result yet. This is a state
  `cagents` itself decides, not something Claude reports — see §2.
- **Done** — a human has explicitly accepted the result.
- **Stopped / Failed** — ended without completing normally.

## 7. Creating and attaching to a session

Starting a new session should take almost no setup — at most a short optional label for your own
reference. `cagents` should not ask you to pre-compose a task description in a form; you talk to
Claude directly, in a real conversation, from the first message. Whatever name Claude's own session
naming eventually settles on should just appear — `cagents` never needs to be told a session's name
or keep it in sync by hand.

Attaching (however it's implemented under the hood) must feel identical to running Claude directly —
same terminal, same session, same everything. Leaving that session, however you leave it, must never
end the underlying conversation unless you explicitly stopped it.

## 8. What a session's row/detail should communicate

At a glance, in the list: enough to tell what it is and whether it needs you (name, state, how long
it's been running, which project). In more detail, without attaching: the real substance of the
conversation so far — what was actually said — plus whatever lightweight project context (git state,
branch) is cheap to show.

## 9. Local state

The only things `cagents` should ever need to remember on its own, beyond what Claude already
reports:

- Whether a completed session has been reviewed by a human, and when.
- An optional short note.
- Lightweight relationships between sessions that `cagents` itself created (e.g. one session
  superseding another), if and when that becomes a real feature.

Nothing here should ever need to be kept in sync with Claude's own state by hand — if it can be
derived from what Claude already reports, it shouldn't be stored separately at all.

## 10. Deferred directions (real ideas, not now)

Worth doing once §4 is solid, roughly in the order they'd likely matter:

- A safe, obviously-labeled way to ask something in natural language and have it act on your
  sessions (find, archive, organize) — once the cost/latency of doing so can be made invisible to
  the core loop.
- Recommending stale or superseded sessions for archival.
- Handing a running session off to a fresh one, or branching a session into two.
- Grouping sessions by something other than their directory.

None of these should be picked up by assuming they're wanted — check back against §4 and §5 first.

## 11. How to judge a proposed change

Before adding anything, it should be possible to answer yes to all of:

- Does this make opening, browsing, or attaching to a session slower, less predictable, or more
  likely to fail? If yes, it doesn't ship yet, regardless of how useful it is.
- Is `cagents` the right owner of this, or is it something Claude Code already does that `cagents`
  would just be duplicating?
- If this fails, does it fail loudly and specifically, or could it look like "the app is broken"
  when the person hits it? Silent, generic failure in the core loop is the single worst outcome
  `cagents` can produce.
