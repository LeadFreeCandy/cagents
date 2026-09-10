# Recap line — proposal

Status: design only. The setting and expanded conversation rows are not shipped
in this change.

The proposed **Recap line** setting defaults to **off**. When enabled, every
conversation in the queue, grouped list, and sidebar gets one indented line:

```text
 ✳ ◆ Retry handling         review
     Added retry handling; tests pass · Review changes
 › ◉ Deployment             needs you
     Deployment prepared · Respond to the permission prompt
```

The examples assume those outcomes appear in the conversation. A recap must not
turn an attempted edit or test into a claim that it succeeded.

## Recommended generation: cached summary plus live action

Use a small summarizer to describe the task and latest outcome in roughly 15–25
words. Combine that cached text with an action clause derived directly from
cagents' current state and any actual permission/question prompt. Examples:
“Review changes”, “Answer the question”, “Working; no action yet”, “Waiting on PR”,
and “Done; no action needed”. A suspended done session can say “Done; hover to resume”.
The summarizer should never decide whether a permission request is still pending.

cagents already computes `did_line` and `needs_line` from the last assistant text
and detected session state. Those provide an immediate fallback. Simple excerpts
alone are inexpensive, but often start with “Done”, a Markdown heading, or a code
fragment; a semantic summary is more useful when scanning many conversations.

Feed the summarizer a bounded excerpt: the latest user request, recent assistant
responses, and relevant tool results. Exclude hidden reasoning. Return a single
plain-text summary in a structured response, enforce a length limit, and render it
as literal text. Treat transcript instructions as quoted data. The worker needs
no tools or write access and must not send an extra turn into the real session.

## Triggers, freshness, and memory

- Generate after a completed turn or a stable new blocking question. Debounce
  updates; token streaming, polling, hovering, and changing layout do not trigger
  generation for an unchanged conversation.
- Cache on disk by conversation ID, meaningful turn/content signature, and
  summarizer version. Include the generation time. Reuse after dashboard restarts
  and reject results that finish after the conversation has moved to another turn.
  File mtime alone is unsuitable: resume and bookkeeping can touch transcripts.
- Update the action clause immediately when state changes, without a model call.
  During a new turn, use current task/tool information or explicitly label a cached
  summary “Previous turn”; avoid presenting an old success as the current outcome.
- Use one bounded worker queue, with at most one pending job per conversation and
  a retry backoff. Read small transcript slices, then release them. Keep only small
  strings and cache keys in the UI, with a bounded in-memory cache.
- When first enabled, show deterministic fallback lines immediately and summarize
  visible conversations first. Defer historical/offscreen rows until viewed; do
  not launch a backfill over every stored conversation. A cached recap of a
  suspended conversation never requires reattaching its agent.
- Turning the setting off stops scheduling, cancels queued work, and restores the
  original one-line rows. Generation failures retain the fallback and do not retry
  on every refresh.

A separate short-lived summarizer can use a small Claude or Codex model. It adds
token usage and temporary process memory, even through the user's existing CLI
login. Give recap generation its own provider/model choice rather than inheriting
each conversation's potentially large model. Start with the user's configured
provider unless they override it; don't hardcode a dated model name.

## List behavior

Keep each conversation and its recap in one selectable row. Hovering either line
selects the same conversation; keyboard movement still advances one conversation.
Selection and scroll anchoring must survive recap arrival and setting changes.
Indent the recap under the title, use muted text with a clearer action clause,
and truncate to one line rather than allowing arbitrary wrapping. In a narrow
sidebar, reserve room for the action clause and shorten the summary first.

## Implementation checkpoints

1. Add `Recap line` off by default and a deterministic fallback for every state.
2. Add the optional summarizer, bounded queue, persistent cache, and freshness rules.
3. Verify completed/blocked/working/done transitions, late result rejection,
   disabled behavior, hover/keyboard mapping, and large lists of suspended sessions.

The recommended end result is the hybrid above. A rule-only version would have no
model cost, but would mostly repeat state badges and first-line excerpts rather
than explain what happened.
