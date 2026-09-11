# cagents

A lightweight terminal supervisor for Claude Code and Codex sessions. The agent owns execution; `cagents`
owns visibility and human review state. See `SPEC.md` for what this must do and why; this
file describes what's actually implemented. `OPEN_QUESTIONS.md` records where the
implementation makes judgment calls.

## Install

Requirements (mandatory):

- **macOS** with **tmux** ≥ 3.2 and **git** — sessions live in tmux; cagents is a tmux supervisor.
- **Python 3.10+**.
- **Claude Code CLI** (`claude`) and/or **Codex CLI** (`codex`) on your PATH, signed in.
  Creating and forking Codex sessions requires the current standalone Codex installation
  with `codex app-server daemon start` support.
- **terminal-notifier** — desktop notifications (on by default). Without it cagents falls
  back to `osascript`, which is unreliable, unbranded, and can't do click-to-select:

  ```sh
  brew install terminal-notifier
  ```

  macOS asks for notification permission the first time one fires; if you miss the prompt,
  allow it under **System Settings → Notifications → terminal-notifier**.

Optional, used automatically when present:

- **gh** (GitHub CLI, authenticated) — PR association (`o`) and the waiting-on-PR watcher (`w`).
- **lazygit** — the diff tab becomes a real interactive diff viewer instead of a pager.
- **zoxide** — smarter directory jumps in the new-session shell-pick (`ctrl+o`).

Setup:

```sh
git clone https://github.com/LeadFreeCandy/cagents.git
cd cagents
python3 -m venv .venv
.venv/bin/pip install -e .
ln -s "$PWD/.venv/bin/cagents" ~/.local/bin/cagents   # or anywhere on your PATH
```

Then run `cagents` from a terminal (inside or outside tmux — it builds its own tmux
container either way). Settings live in the in-app panel (`,`); desktop notifications can
be toggled there.

## What it does

- Shows the Claude Code and Codex sessions **you chose to track** — never everything on the machine.
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

Native idle status and recorded turn completion take precedence over stale tool
calls or submit hooks. Claude's runtime status is cached for up to 10s, but newer
transcript or hook activity invalidates that cached answer. Long-running quiet
tools remain working until there is evidence that the turn ended or needs input.

| State | Meaning | How it's detected |
|---|---|---|
| working | Claude or Codex is running a turn | native busy/active status, a current spinner, or ongoing transcript activity/tool calls without a later completion |
| needs input | blocked on a human | native waiting status or a live permission/question prompt |
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
  and a command prompt (`:`).
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

`Ctrl+G` returns to the top conversation in the queue from inside a conversation
or workspace tab, including full-width chat. Focus returns to the queue; press
Enter to enter that conversation. With the list focused, `g` / `G` jump to its
first / last conversation.

## Install / run

```sh
cd ~/Documents/projects/cagents
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/cagents            # or add an alias / symlink onto your PATH
```

Options: `--claude-dir` (default `$CLAUDE_CONFIG_DIR` or `~/.claude`), `--store`
(default `~/.local/share/cagents/state.json`).

## Using Codex

After updating, run `.venv/bin/pip install -e .` and restart cagents.

- Press `n`, choose your directory in the shell, then type `codex`. Model and other
  CLI options work, for example `codex --model <model-name>` or `codex --cd ../project`.
- Press `a` to track an existing conversation. The picker labels each provider.
- Enter attaches to the native Codex terminal, resuming the exact conversation when
  needed. `codex resume <UUID>` also works in a cagents shell.
- `f` forks through Codex's local app server. `h` lets you choose Claude or Codex
  and a model for the successor. A Codex source supplies the spec using a bounded
  transcript excerpt and a separate read-only Codex invocation. Review, snooze,
  PR watching, diff comments, and optional full-history search also work with Codex.
- Use `--codex-dir /path/to/codex-home` to override `$CODEX_HOME` or `~/.codex`.
  New/fork operations require a standalone installation under that home too.

cagents reads Codex's rollout files without modifying them. It uses explicit turn
events and, when available, the local daemon's status to detect work and approval
requests. New threads come from the app server; cagents never guesses their IDs.
Before opening an empty thread's terminal, cagents asks Codex to persist a short
integration context note. This prevents an immediate resume failure and starts no
model turn.
Codex retains its configured model and permissions unless you supply CLI overrides.
Managed resumes require an explicit UUID; use `a` to pick a conversation by title.
Utility commands such as `codex login`, `codex exec`, and `codex --help` stay in the shell.

Codex tracking is local to cagents; the existing cagents2 bridge continues to share
Claude conversations. Claude-specific hook and background-task signals remain
Claude-specific. The composer-aware arrow probe recognizes Claude's composer; with
Codex it passes through unfamiliar layouts, and the explicit layout shortcuts still work.

Integration references: [Codex CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
and [Codex app server](https://learn.chatgpt.com/docs/app-server).

Expanded conversation rows show a one-column provider icon after the state: `✳` Claude or `›` Codex.
Titles follow the provider's saved conversation name, including native renames.
Codex's local name index works even while a conversation is suspended; Claude's
saved custom/generated titles are read from its transcript. An explicit cagents
label takes priority, with the first real prompt as the fallback for unnamed threads.
Settings (`,`) → **Conversation title width** controls the expanded queue/grouped
title limit, now **22** columns by default (previously 44; configurable from 8–120).
Collapsed sidebars show only the status icon and conversation name, using all
remaining width for the name and truncating it with an ellipsis when necessary.
Expand the sidebar to see provider, age, and status labels such as **Done (auto)**.

In tmux, managed Codex launches and restarts inherit the dashboard terminal's
foreground/background colors before starting, preserving native message and
composer shading even when the conversation starts detached.

Scrolling uses native tmux mouse handling. Programs that request mouse input
receive the wheel directly. Otherwise, the wheel scrolls terminal history one
line per event, starting with the first tick; scrolling back to the bottom or
pressing `q` returns to live output. Codex's main view uses this terminal history.
Cagents does not open its transcript viewer or send substitute CLI keystrokes.
Managed Codex launches respect its normal terminal mode.

Drag-select text and release to copy directly to the macOS clipboard. The
highlight stays visible after release; `q` dismisses it, and `⌘V` pastes into
the conversation. An existing custom tmux copy command is preserved. Clipboard
updates from native CLIs still pass through every nested terminal. Pasting while
scrolled back returns to the live prompt and preserves multiline paste as one
paste, without submitting it. Unbound typing keys also return to the prompt;
tmux's scrollback navigation and selection keys remain available.
The proposed, default-off **Recap line** feature is described in
[the recap design](RECAP_DESIGN.md); it remains pending in [TODO.md](TODO.md).

## What it stores

One small JSON file (`~/.local/share/cagents/state.json`): tracked session ids, the project
directory each was added from, an optional label/note, and the reviewed-at timestamp.
Codex keys use `codex:<thread-id>`; existing Claude IDs and bookkeeping remain compatible.
Agent transcript data is strictly read-only to cagents; native agents own conversation changes.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # unit, UI, and isolated tmux/PTY regression tests
```

The macOS clipboard tests are opt-in because they exercise the system clipboard.
This harness restores all original clipboard items and formats afterward (requires
Swift from the Xcode command-line tools):

```sh
CAGENTS_MAC_CLIPBOARD_TESTS=1 swift tests/live/preserve_clipboard.swift \
  "$PWD/.venv/bin/python" -m pytest -q tests/test_clipboard.py -k updates_mac
```

Layout: `claude_data.py` (read-only parsing of Claude's store), `tmuxctl.py` (tmux on the
`claude` socket), `store.py` (cagents' own state), `sessions.py` (state derivation +
tmux↔session mapping), `format.py` (pure renderables), `views.py` / `modals.py` / `app.py`
(Textual UI).
`codex_data.py` reads Codex rollouts; `codex_rpc.py` handles local thread creation,
forking, status reads, and explicitly requested one-shot generation.

## Idle conversations and restarts

Settings (`,`) includes **Auto done duration**, default **7d**. Conversations
without UI interaction or new conversation activity become **Done (auto)**.
Existing timestamps are used on the first launch after updating, so old
conversations qualify immediately. Choose off, 1d, 3d, 7d, 14d, or 30d; custom
positive durations such as `12h` also work in `auto_done_duration` in state.json.
Actively working and explicitly snoozed conversations are left alone.

Manual and automatic done conversations share the Done group, newest completion
first. Hovering or selecting an automatic one resets its inactivity timer; `d`
can also return it to the queue. New conversation activity reopens it.

Done conversations with no interaction for **one hour** suspend their native
Claude/Codex process (checked every 30 seconds). Their history and terminal tabs
remain available. A **☾** marks a suspended conversation; hovering or selecting
it resumes the same conversation. Background refreshes leave it suspended.

**Ctrl+R** in the list stops and restarts the selected agent with its saved
conversation. **`:restart`**, then Enter, restarts running tracked agents and
reloads the cagents dashboard. Both interrupt in-flight work and reload the
installed CLI. Suspended conversations stay asleep during `:restart`. Shell tabs
and conversations outside cagents' tracked tmux panes are preserved.

For Codex terminals connected to a shared local app server, cagents interrupts
only that thread and stops its background terminals before closing the TUI.
The shared daemon stays running; server-side thread memory follows
[Codex's own idle-unload policy](https://learn.chatgpt.com/docs/app-server).
Restarts of saved conversations use a fresh local Codex runtime. A new thread
without a saved first message retains its existing server connection.


## cagents3 native panes

Run `cagents3` from the main checkout to use the single-server native
pane backend. It has separate bookkeeping and keeps the existing `cagents`
launcher available. Conversation panes and drafts survive selection, tab changes,
and dashboard relaunch. See [CAGENTS3_DESIGN.md](CAGENTS3_DESIGN.md) for layout,
compatibility, and test details.
