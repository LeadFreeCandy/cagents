# Using cagents — the guide

Two commands exist on your PATH:

| command | what it runs | store |
|---|---|---|
| `cagents` | stable v0.1.0 (git `main`, checkout `~/Documents/projects/cagents-stable`) | `~/.local/share/cagents/state.json` |
| `cagents-feature` | everything in flight (`feature/todos-and-diffs`) | `feature-state.json` (seeded from stable's on first run) |
| `cagents-shell` | `cagents-feature` inside the persistent sidecar container (see below) | same as feature |

They keep separate stores on purpose: the feature schema has fields (todos, archived)
that the stable build would silently drop on save.

## The mental model

Claude Code runs your sessions (inside tmux, via your `claude` wrapper). cagents is a
viewport over them plus the one thing Claude doesn't track: whether *you* have looked at a
finished result. Nothing you do in cagents can lose a conversation — attach hands you the
real CLI, untrack/archive only touch cagents' own bookkeeping, and Claude's files are
strictly read-only to it.

## Daily loop

1. `cagents-shell` (or `cagents-feature` for full-screen mode).
2. The **grouped view** (`1`) is home: sessions by project, preview of the real
   conversation on the right, refreshed every 2s.
3. Glance at the header: `◉ needs you` and `◆ review` counts are the work.
4. `2` (queue) sorts everything by who needs you first. Walk it top to bottom.
5. For each: `space` to peek (read the transcript without attaching), `D` to review the
   diff, `enter` to actually get in, `r` when you've accepted the result.

## Getting in and out of a session

**The rail is the default.** Run `cagents-feature` in a plain terminal and it wraps
itself in the sidecar container automatically — `enter` opens the session in a right
pane and the list stays put as a left rail. (`cagents-shell` is now just an alias.)

- **In**: `enter` — the real Claude CLI in the right pane.
- **Back to the list**: **`←` (left arrow)** — closes the session pane and drops you on
  the full-width list; the session keeps running in the background. This replaces
  Claude's empty-prompt ← (the agents panel); the trade-off is that ← no longer moves
  the cursor while editing text in the Claude prompt — toggle it off in settings (`,`)
  if that bites. **`ctrl+\`** toggles rail ↔ session without closing anything, and
  works in every terminal. `alt+q` / `alt+w` jump one-way (need Option-as-Meta /
  "Esc+" in the terminal profile). Clicking a pane also works; the rail
  auto-expands/collapses with focus.
- **Classic full-screen mode**: `cagents-feature --fullscreen` (and stable `cagents` is
  always this). There, attach takes the whole terminal and you come back with
  `ctrl-b d` (tmux detach). Detaching never stops Claude.
- **Inside your own tmux**: cagents splits your current window instead of nesting a
  container. Your tmux has no auto-resize hooks, so get back with a click or
  `ctrl-b` + arrow, and press `=` in cagents to grow the rail back out.
- Attaching to a `stopped` session resumes it (`claude --resume`) in a fresh tmux session
  first, so enter always lands you in a live CLI.

## Sidecar mode — cagents as a permanent fixture

```
┌──────────────┬──────────────────────────────────────┐
│ cagents rail │  the real Claude session             │
│ (34 cols,    │  (a live tmux attach — same session  │
│  states      │   your `cs` command would show)      │
│  ticking)    │                                      │
└──────────────┴──────────────────────────────────────┘
```

- `enter` on a session opens/replaces the right pane; the rail collapses to 34 columns
  and switches to dense rows (glyph + title + age — states still update live).
- **`ctrl+\`** — toggle rail ↔ session (works everywhere). **`alt-q`** — to the list,
  **`alt-w`** — to the session (need Option-as-Meta in the terminal profile). The rail
  expands to half the screen when focused, collapses when you leave; mouse clicks do
  the same thing.
- The right pane is a *nested* tmux client onto your normal `claude` socket. Close the
  pane, quit the shell, reboot — the session is untouched and still visible to `cs` and
  plain `cagents`.
- One quirk of nesting: `ctrl-b` talks to the container, so to send a prefix to the
  *inner* tmux you'd press `ctrl-b ctrl-b`. You almost never need to — Claude itself
  uses plain keys, and alt-q/alt-w replace the detach dance entirely.

**Why Esc isn't the back key (and how to make it one).** Inside a session, Esc is
Claude's interrupt — the single most important key when a turn goes sideways. A tmux-level
Esc override would eat it before Claude ever saw it. If you truly want it, uncomment the
marked line in `~/.local/bin/cagents-shell`:

```
# t bind -n Escape select-pane -t :.0
```

Everything still works — you just lose interrupt-by-Esc in sessions (you'd click the
pane or use `ctrl+c`-free alternatives instead). Alt-q is the recommended trade.

## Views

| key | view | what it's for |
|---|---|---|
| `1` | Grouped | home base: sessions by project + live preview |
| `2` | Queue | triage: sorted needs-you → review → working → stopped → done |
| `3` | Kanban | pipeline shape: columns by state, `h`/`l` between columns |
| `4` | Todos | intent: todos that spawn sessions and worktrees |

`tab` cycles. Each view keeps its own cursor across refreshes.

## Session states (derived fresh every ~2s, never stored)

- `● working` — live turn: pane shows activity or the transcript was just written.
- `◉ needs you` — blocked on a human: permission prompt, question, or you're at the prompt.
- `◆ review` — Claude finished; **no human has looked yet**. This is cagents' own state.
- `✓ done` — you pressed `r` (or accepted via peek/palette) *after* the last activity.
  If Claude does more work later, it drops back to `◆ review` automatically.
- `■ stopped` — no live process and the transcript ends mid-turn.

Row markers: `⇄` someone is attached · `✎` has a note · `⇗` recorded a PR/artifact link ·
`⑂N` background agents · `⎇` (todo rows) has a worktree.

## Acting on a session

| key | action |
|---|---|
| `enter` | attach (the core loop — always the real CLI) |
| `space` | peek: full-screen transcript, `r` inside marks reviewed, `esc` closes |
| `D` | diff review (below) |
| `o` | open the newest recorded link (PR / artifact) in the browser |
| `r` | toggle reviewed ↔ needs review |
| `e` / `L` | note / label (label overrides the AI title in lists) |
| `x` | untrack (cagents-only; Claude's data untouched) |
| `n` / `a` | new session / track an existing one |

## Settings (`,`)

Three toggles, applied immediately and persisted:

- **Sidebar rail** (on) — off returns to classic full-terminal attach everywhere.
- **Toast notifications** (off) — bottom-right popups for routine events. Errors and
  warnings always show regardless.
- **Left arrow returns to list** (on) — the ← capture described above. Works in both
  modes: with the sidebar it closes the session pane; in full-screen mode it detaches
  the attach (session keeps running) and cagents resumes. The full-screen binding is
  scoped to cagents' own tmux client, so Left in your other attached clients (`cs`,
  phone, other terminals) is untouched.

## Todos (`4`)

The unit is intent, not process. `A` adds a todo (text + optional project directory).
Then, with a todo selected:

- `n` — start a session for it (directory prefilled, auto-linked).
- `W` — grow it a **worktree**: `<repo>-worktrees/<slug>` on branch `todo/<slug>`, with a
  session already running inside. The todo row shows `⎇`.
- `enter` — attach to its newest session. The row shows its sessions' live states.
- `d` — done. If it spawned workspaces you're offered the archive: linked sessions
  disappear from the session views (history kept in the store), and a *clean* worktree is
  removed — a dirty one refuses loudly and stays. `d` again reopens and un-archives.
  Done todos drop below a `── done ──` divider, greyed out — a visual archive.
- `x` — delete the todo only.

When a todo's newest session is waiting on you, two single-line rows appear under it:
**`did`** — the agent's most recent statement (from the transcript, no summarizing) —
and **`needs`** — the actual question from a permission prompt, or "your review".
While the agent is working the rows are absent entirely; nothing there would be current.

## Diff review (`D`)

Everything the selected worktree changed: committed + uncommitted vs the default branch's
merge-base, untracked files shown as additions. Inside:

- `j`/`k` move the cursor, `n`/`p` jump between files.
- `c` — comment on the cursor line (shows inline, anchored to file:line).
- `g` — pull the branch's PR review comments from GitHub (`gh` must be authed); inline
  comments anchor to their diff lines, review verdicts float to the top. Deduped on re-pull.
- `s` — send every comment (yours + GitHub's) into the session's Claude as one message,
  pasted into the real CLI prompt via tmux. A dead session is resumed first.
- `esc` — close (drafts are not persisted — send before you leave).

## Fleet palette (`:`)

Plain English about your *fleet*, not your code: "mark everything in dealpilot reviewed,
it's merged". Claude gets a read-only snapshot of the session table and returns a plan —
whitelisted actions on cagents' own bookkeeping only, each with a reason. Nothing applies
until you press `y`. Garbage replies fail loudly. Expect ~10–20s; it runs in the
background and never blocks the list.

## When something looks wrong

- **A working session shows "needs you"** (or vice versa): state is heuristic at two
  edges (see OPEN_QUESTIONS.md). It self-corrects on the next pane/transcript signal;
  `R` forces a refresh.
- **"Attach failed" / "transcript missing"**: loud and specific by design — the message
  names the real problem (dir deleted, tmux gone, session file removed).
- **Sidecar rail stuck narrow**: press `alt-q` (any focus change re-runs the resize hook).
- Stores live at `~/.local/share/cagents/` — plain JSON, safe to read, edit, or delete
  (deleting only forgets tracking/review/todos, never conversations).
