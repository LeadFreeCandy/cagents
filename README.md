# cagents

A lightweight terminal supervisor for Claude Code sessions. Claude owns execution; `cagents`
owns visibility and human review state. See `SPEC.md` for what this must do and why; this
file describes what's actually implemented. `OPEN_QUESTIONS.md` records where the
implementation makes judgment calls.

## What it does

- Shows the Claude Code sessions **you chose to track** — never everything on the machine.
- Three views over the same sessions:
  - **Grouped** (`1`) — sessions grouped by project directory, with a live preview pane
    showing the real conversation tail (parsed straight from Claude's own transcript, never a
    summary).
  - **Queue** (`2`) — one flat list, ordered by who needs your attention first:
    *needs you → needs review → working → stopped → done*.
  - **Kanban** (`3`) — columns by lifecycle state; `h`/`l` move between columns.
- **Enter attaches to the real Claude CLI.** Sessions live in tmux on the dedicated `claude`
  socket (the same one your `claude-tmux` wrapper uses), so attaching is a real
  `tmux attach` and detaching (`ctrl-b d`) never kills anything. Attaching to a stopped
  session resumes it (`claude --resume`) inside a fresh tmux session on that socket.
- Tracks the one thing Claude can't: **whether a human has reviewed a finished session**.
  `r` marks the selected session reviewed (state becomes *done*); if Claude does more work
  afterwards, the review automatically goes stale and the session returns to *needs review*
  — the reviewed timestamp is compared against the transcript, never synced by hand.

## Session states

Derived fresh on every refresh (~2s), never stored:

| State | Meaning | How it's detected |
|---|---|---|
| working | Claude is doing something | live pane shows a running turn, or the transcript was written to in the last ~20s |
| needs input | blocked on a human | live pane shows a permission/question prompt, or an unanswered tool call with no recent writes |
| needs review | Claude finished; no human has looked | last turn completed, no review newer than the last activity |
| done | a human accepted the result | reviewed at/after the last activity |
| stopped | ended without completing | no live tmux session and the transcript ends mid-turn |

Liveness comes from tmux: sessions started through `cagents` carry a `CAGENTS_SESSION_ID`
tmux environment variable and map back exactly; sessions started outside it are matched by
working directory + transcript recency.

## This branch (feature/todos-and-diffs)

On top of v0.1.0 (which lives on `main`, runnable as `cagents`; this branch is
`cagents-feature` with its own store):

- **Peek** (`space`), **badges** (`⇗` links / `Δ` files touched / `⑂` agents, `o` opens),
  **fleet palette** (`:`) — promoted from the cagents-next prototypes.
- **Todos (view `4`)** — units of intent. `A` add; `n` starts a session for the selected
  todo (linked, prefilled); `W` grows it a dedicated **git worktree** (branch
  `todo/<slug>`, sibling `<repo>-worktrees/<slug>` dir) with a session inside; `enter`
  attaches to its newest session; the row shows its sessions' live states. `d` completes
  the todo and offers to **archive the workspace**: linked sessions hidden from the views
  (history kept), a clean worktree removed — a dirty one refuses loudly. Reopening
  un-archives.
- **Diff review (`D`)** — everything the selected worktree changed (committed + uncommitted
  vs the default branch, untracked included), pretty and line-numbered. Move the cursor,
  `c` to comment on a line, `g` to pull the PR's review comments from GitHub (`gh`),
  `s` to send the whole comment set into the session's Claude — pasted into the real CLI
  via tmux (resuming the session first if it's not running).

## Keys

`?` inside the app shows the full list. The short version: `1/2/3` views, `j/k` move,
`enter` attach, `n` new session, `a` track an existing one, `r` reviewed, `e` note,
`L` label, `x` untrack, `q` quit.

## Install / run

```sh
cd ~/Documents/projects/cagents
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/cagents            # or add an alias / symlink onto your PATH
```

Options: `--claude-dir` (default `$CLAUDE_CONFIG_DIR` or `~/.claude`), `--store`
(default `~/.local/share/cagents/state.json`).

## What it stores

One small JSON file (`~/.local/share/cagents/state.json`): tracked session ids, the project
directory each was added from, an optional label/note, and the reviewed-at timestamp.
Nothing else. Claude's own data is strictly read-only to cagents.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # unit + pilot-driven UI tests, no real tmux/~/.claude touched
```

Layout: `claude_data.py` (read-only parsing of Claude's store), `tmuxctl.py` (tmux on the
`claude` socket), `store.py` (cagents' own state), `sessions.py` (state derivation +
tmux↔session mapping), `format.py` (pure renderables), `views.py` / `modals.py` / `app.py`
(Textual UI).
