# Using cagents (v2) — the guide

Run `cagents` from any terminal. It wraps itself in a tmux container: the
session list as a left rail, and one viewer pane on the right that always shows the
real thing.

## The one idea everything follows from

**The right pane is never a re-implementation.** For a live session it is a real tmux
attach — actual Claude Code, pixel for pixel, streaming. For a dead session it's the
transcript, rendered read-only in the same pane. `enter` doesn't "open" anything; it
just moves your focus into the pane that's already showing the session. Preview and
attach cannot disagree because they are the same mechanism.

Moving through the list re-points the pane (debounced ~250ms so j/k stays instant).
Hover the pane and scroll with the mouse wheel.

## Layout — the arrows size the Claude pane

Three states, ordered by Claude's width; `←` shrinks one step, `→` grows one step:

```
WIDE (50/50, you're in the list)  ←→  SMALL (slim rail, you're in the session)  ←→  HIDDEN (full width)
```

Clicking a pane also moves focus; the rail auto-sizes to follow. The trade-off, as
accepted: the arrows don't move the text cursor while editing in the Claude prompt
(toggle off in settings if it bites; you lose the layout keys).

## The right side is tabbed

The pane hosts three tabs (a clickable bar across its top), left to right:

```
  session    diff    term-1
```

- **session** — the live Claude attach / transcript (what browsing drives).
- **diff** — **this worktree versus master**: committed + uncommitted, from the
  merge-base with the first of origin/HEAD, origin/main, origin/master, main,
  master (remote refs first — a linked worktree often has no local main).
  Rebuilt fresh every time you open it — `ctrl+d` or clicking the tab. The
  settings panel can switch it to uncommitted-only mode.
- **term-1** — a persistent shell, started in your launch directory; it keeps its
  state across tab switches and is only recreated (in the session's dir) if it died.

Opening a tab takes the whole pane — the conversation is hidden until you switch
back (click the tab, or `enter` from the list).

## Anywhere keys — they work while you're inside the session

- **`ctrl+d`** — build the diff for the selected session and switch to the diff tab.
  Not a repo → says so, stays put. (Accepted collision: Claude's
  quit-on-empty-prompt; `/exit` still works.)
- **`ctrl+s`** — switch to the terminal tab.

Both are driven by a context file that follows your selection, so they always act on
the session you're looking at. In `--fullscreen` mode (no tabs) they fall back to a
popup diff and a split shell.

## Session states

- `● working` — a real turn in flight (pane markers or a conversation record in the
  last ~20s). Also shown, with "active outside cagents' tmux", when the transcript is
  being written by a host cagents can't see (cmux, bare terminal) — Enter refuses to
  spawn a duplicate CLI on those.
- `◉ needs you` — a real dialog (permission/question) or idle at the prompt.
- `◆ review` — finished, no human has looked. `d` marks **done** (toggle).
- `◎ monitoring` / `◌ background` — idle, but Claude's own Monitor is watching /
  a backgrounded command is still running. Low priority: below review, above
  working. Tracked through the tasks' real lifecycle: they persist across new
  messages and only end when the task's completion/timeout notification lands
  (monitors also expire at their declared timeout). No keybind.
- `⧖ waiting` — you pressed `w`: done here, parked on its PR. Auto-found from the
  branch via `gh` (or you paste the URL). Polled every ~5 min: **new PR
  comments → back to `◆ review` marked "github comments"; merged → `✓ done`
  marked "merged"**. New local activity also un-parks it.
- `✓ done` — accepted (`d`). Claude doing more work re-alerts it automatically.
- `■ stopped` — not running, transcript ends mid-turn.

## List keys

| key | action |
|---|---|
| `enter` | the session tab, focused (resuming the session first if dead, on cagents' private socket) |
| `d` | done / un-done |
| `w` | waiting on external (PR watch) |
| `f` | fork: new session continuing this conversation; you type its first prompt; named after it |
| `h` | handoff: the old session writes a spec (on a throwaway fork), a fresh session starts on it, the old one is marked done (`d` restores) |
| `*` | related: parent / siblings / children of forks & handoffs; jump to one |
| `D` | full diff-review screen: comment on lines, pull GitHub PR comments, send all comments into the session's Claude |
| `o` | open the session's PR/artifact link — if none is recorded, prompts you to paste one (remembered for next time and for `w`) |
| `R` / `x` | rename (display name) / untrack (cagents bookkeeping only) |
| `n` / `a` | new session (launch-dir default, tab completes/cycles, `ctrl+t` picks the dir in a real shell — zoxide works) / track existing |
| `1 2 3` `tab` | queue (default) / grouped / kanban (←/→ move kanban columns) |
| `:` | fleet assistant: plain English → a confirmed plan on cagents' bookkeeping |
| `,` `?` `q` | settings / help / quit |

## Reliability guarantees (each one earned the hard way)

- Sessions cagents starts live on a **private tmux socket** — starting a claude next
  to a live one on a shared socket crashes it (reproduced 6/6). Your wrapper's
  sessions on the `claude` socket are still discovered and attached.
- A tmux session in a *parent* directory only maps to a transcript if its pane
  **actually displays that conversation** — wrong-session attaches can't happen;
  worst case something live shows as dead.
- Sessions cagents spawns carry **Claude Code's own hooks** (Notification / Stop /
  UserPromptSubmit) stamping a per-session events file — the authoritative state
  signal. Validated against a live haiku session: zero false "needs you" through a
  35s silent foreground tool; Stop flips to review instantly.
- For sessions cagents didn't spawn: "working" runs on the conversation clock
  (records, not file mtime), "needs you" requires a real on-screen dialog plus a
  debounce, and an unanswered tool call without a visible dialog counts as a tool
  still *running* — never a guessed permission prompt (that guess was the
  intermittent false "needs you", replicated live and removed).
- A crashed container is detected (pane 0 must be cagents) and rebuilt, never
  reattached.
- `cagents --reset` wipes cagents' bookkeeping (confirm-gated). Claude's transcripts
  are never touched by anything cagents does.

## Settings (`,`)

Sidebar rail (on) · toast notifications (off; errors always show) · arrow layout keys
(on) · desktop notifications (off; with terminal-notifier installed, clicking one
selects the task).

## Modes

- No tmux, plain terminal → auto-wraps in the container (the default experience).
- Inside your own tmux → splits your current window instead.
- `--fullscreen` → classic whole-terminal attach with a cagents statusline;
  ← detaches back to the list (scoped to cagents' own client only).
