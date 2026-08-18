# Decision log

The context behind how cagents got this way — distilled from the build conversation
(Claude Code session `4e1578b7`, August 17, 2026, working from `SPEC.md`). Read this
before relitigating anything; most of these choices were made against a real
constraint, not taste. Newest epochs last.

## 1. Foundation (v0.1.0, `main`)

- **Python + Textual, not Go + Bubble Tea.** Bubble Tea was the default instinct, but
  the machine had no Go toolchain and Textual won on merits anyway: batteries-included
  widgets, CSS-ish styling, `App.suspend()` for terminal handoff, and above all a
  first-class headless test harness (`run_test()` pilot) — which is why the project has
  ~180 UI-level tests instead of prayers.
- **tmux is the attach mechanism because the user already lives there.** Discovery:
  `claude` is aliased to `~/.claude/bin/claude-tmux`, which runs every session on a
  dedicated socket (`tmux -L claude`), with `cs` to attach (including from a phone).
  So cagents never spawns/owns Claude processes: attach = `tmux attach` on that socket;
  liveness = "does the tmux session exist". Detach can never kill work.
- **Session data is read straight from `~/.claude/projects/*/*.jsonl`** — format
  reverse-engineered from real transcripts, not docs. Bounded head+tail reads (a 9MB
  transcript parses in ~4ms); the full-file read was rejected to protect spec §4.1
  ("open instantly").
- **tmux↔session mapping is tiered**: exact via a `CAGENTS_SESSION_ID` tmux env var
  (sessions cagents creates), then pane-cwd == transcript-cwd, then pane-cwd is an
  *ancestor* of transcript-cwd. The ancestor tier exists because live testing caught
  the user's real layout: `claude` launched from `~` for a session working in
  `~/Documents/projects/cagents` — exact matching called it dead.
- **State is always derived, never stored** (spec §9). The only persisted things are
  tracking, reviewed-at, note/label — later todos, settings, lineage. "Reviewed" is a
  timestamp compared against the transcript so review **goes stale automatically**
  when Claude does more work; nothing is ever synced by hand.
- **capture-pane target quirk**: `=name` resolves for attach/has-session but NOT for
  pane targets on tmux 3.6a — must be `=name:`. Cost an hour; documented in code.

## 2. The prototypes (cagents-next) and the organizing principle

- **"Claude writes it, cagents shows it."** The rule for growing with Claude without
  duplicating it: badges come from records Claude already writes (`pr-link`,
  `frame-link`, `pendingBackgroundAgentCount`, Edit/Write tool calls → files touched).
  New record types should be one-line registry additions.
- **The fleet palette (`:`) is the only AI surface in the app**, and it's fenced:
  explicitly invoked, async (never in the core loop), read-only snapshot in, a
  *whitelisted plan* out, human confirmation before anything applies, garbage replies
  fail loudly. Live test: real `claude -p` correctly declined to re-review an
  already-reviewed session. ~18s latency is why none of this ever runs implicitly.

## 3. Two commands, two stores

- `cagents` = `main` (v0.1.0, frozen) via the `cagents-stable` git worktree;
  `cagents-feature` = the feature branch. **Separate state files on purpose**: the
  feature schema adds fields (todos, archived, settings, lineage) that the stable
  build would *silently drop on save*. Sharing one file would let stable eat feature
  data. Feature store seeds from stable's on first run.

## 4. Todos, worktrees, diff review

- Todos are **units of intent** that spawn workspaces: `n` links a session, `W` grows
  a worktree (`<repo>-worktrees/<slug>`, branch `todo/<slug>`) with a session inside.
  Completing a todo offers to **archive** its workspaces — sessions hidden (history
  kept), a *clean* worktree removed, a dirty one refuses loudly.
- The built-in diff (`D`) shows merge-base→working-tree plus untracked files, takes
  line-anchored comments, pulls GitHub PR review comments via `gh`, and **pastes the
  whole comment set into the session's real CLI** via tmux bracketed paste
  (`load-buffer` + `paste-buffer -p`) so multi-line messages don't submit early. Dead
  sessions are resumed first with a fixed ~4s boot wait — known weak point; the proper
  fix is the hooks-as-push-channel idea in `IDEAS.md`.

## 5. Sidecar (the always-present rail)

- **The rail is the default**, after the user hit fullscreen takeover: bare launch
  auto-bootstraps a tmux container (socket `cagents-ui`); inside someone's own tmux it
  splits in place; `--fullscreen` opts out. Rail collapses to 34 cols (compact
  rendering, states still ticking) and follows focus via the `after-select-pane` hook —
  **not** `pane-focus-in`, which needs terminal focus-reporting most terminals lack.
- **Esc is deliberately never bound** as "back": inside a session Esc is Claude's
  interrupt — the most important key when a turn goes wrong. The user asked for Esc;
  the answer was a commented-out line plus better defaults.
- **`ctrl+\` exists because Alt keys silently fail** on default macOS terminal
  profiles (Option-as-Meta / "Esc+"). It routes through `select-pane` (not
  `last-pane`) because only `select-pane` fires the resize hook — found live.
- **`←` backgrounds the session** (kills the pane; session lives on). Trade-off,
  accepted explicitly by the user and made toggleable: while on, ← doesn't move the
  text cursor in the Claude prompt. In fullscreen mode the same key works via a
  **tty-filtered** `Left → detach-client` binding set only for the duration of the
  attach — other clients (`cs`, phone) keep their ← untouched.

## 6. State-detection hardening (all found by the user in real use)

- **False transient "needs you"**: prompt phrases ("Do you want…") appear in Claude's
  own *output*. Fix: a pane counts as a dialog only with the `❯ 1.` choice-row
  signature AND a phrase; plus a one-refresh debounce on working→needs-input. Cost: a
  real permission prompt shows ~2–4s late. Right trade.
- **False "working" after enter+leave**: resuming/attaching *touches the transcript's
  mtime without appending records* (verified empirically: 13 lines before and after,
  mtime jumped). Fix: freshness runs on the **conversation clock** (last user/assistant
  record timestamp), never file mtime. mtime is still used where it's genuinely right
  (tmux mapping, where the resume-touch actually helps).

## 7. Fork, pause/wake, monitoring, notifications

- **Fork (`F`)**: `--resume <old> --fork-session --session-id <new>` — the flag combo
  was verified against the real CLI before building (forked file materializes on first
  message; fine, since the typed prompt is auto-delivered). Fork is labeled by its
  prompt. Original untouched.
- **Pause (`p`)** input is one field, three interpretations: duration (`2d`) → timer;
  plain English → **Claude writes a read-only wake-check script, shown for approval
  before it's saved** (same trust model as the palette), then run every ~5 min with a
  30s timeout; empty → indefinite. **Auto-pause defaults to 7 days idle** (setting,
  0 disables) so open todos stop nagging forever.
- **Monitoring (`m`)** is a timestamp like reviewed-at, not a flag: `◎` sits between
  review and working in attention order, and *any new activity re-alerts* it back to
  needs-review. Symmetry with reviewed-at keeps it derivable and un-syncable.
- **Desktop notifications are edge-triggered** (transition into needing you; never on
  startup) and off by default; toast notifications are also off by default by request
  (errors/warnings always show — spec §11's "fail loudly" is non-negotiable).
  Click-to-select works via terminal-notifier's `-execute` writing a request file that
  the app polls; plain osascript can't observe clicks.

## 8. Handoff, lineage, plugins

- **Handoff (`H`)**: the *old session writes the spec for its successor* — but on a
  **throwaway fork** (`--resume --fork-session -p`), so summarization never mutates
  the original transcript. New session starts with spec + task as its first message;
  the old one is auto-marked done, restorable with `r` (done is just the reviewed flag,
  so no new mechanism).
- **Lineage** fills spec §9's reserved "lightweight relationships" slot: `parent_id` +
  relation on the tracked session; children/siblings resolved per-snapshot; `↳`/`»N`
  row markers; `*` to browse and jump.
- **Plugins** are Python files in `~/.local/share/cagents/plugins/`, hot-reloaded by
  mtime, declaring keybinds/automations against a deliberate `api` surface. Errors are
  contained per-plugin; cagents' own keys are reserved and refused. `+` spawns/reuses a
  **meta session** (label `meta`, cwd = plugins dir) seeded with the framework docs +
  the request — Claude extends the tool from inside the tool. Accepted risk, stated
  plainly: plugins are arbitrary Python with the user's permissions; that's what an
  extension system is.

## Standing constraints (the ones that keep winning arguments)

1. Core loop above everything: open fast, browse smooth, preview real, attach always
   (spec §4). Anything that risks it doesn't ship.
2. Claude owns execution; cagents owns visibility + human review state. No second
   copies of anything Claude stores.
3. AI features are opt-in, async, confirm-gated, and never in the hot path.
4. Fail loudly and specifically; suppress routine noise, never errors.
5. Derived > stored. If it can be computed from Claude's data, it is.

## Where the full history lives

The complete conversation transcripts are Claude Code sessions on the build machine
(`~/.claude/projects/-Users-samir-Documents-projects-cagents/`, primary session id
`4e1578b7-1526-412c-98dd-2f2d574c764b`). Companion docs in this repo: `SPEC.md` (the
product contract), `README.md` (what's implemented), `GUIDE.md` (how to drive it),
`IDEAS.md` (judged backlog), `OPEN_QUESTIONS.md` (honest unknowns).

## 9. The v2 rebuild (Aug 18)

A second machine (Sonnet-driven) added 13 commits of features on `origin/main` and made
things flakier. Rebuilt from the last-good state (`v2` branch) keeping Sonnet's three
*verified* findings and redesigning around them:

- **Socket collision is fatal** (their repro, 6/6): a claude spawned on a tmux socket
  hosting a live claude dies instantly. v2 spawns on a private `cagents-sessions`
  socket — but unlike their fix, still *discovers/attaches* wrapper sessions on
  `claude` (attaching is only a client; the crash is spawn-only).
- **The viewer pane IS the session.** Browsing points one persistent right pane at a
  real attach (or the transcript when dead); Enter = focus; ← = a 3-state layout
  cycle (small rail → 50/50 list → rail hidden). Preview/attach divergence is
  impossible by construction; the internal Rich preview survives only in
  --fullscreen where there is no tmux to embed.
- **Ancestor-tier mapping must be content-verified** (found live, again): unrelated
  tmux sessions in a parent dir claimed a stale transcript (mtime lined up thanks to
  resume-touch) and Enter attached the wrong session. Tier-3 matches now require the
  pane to actually display text from the transcript (whitespace-normalized so
  wrapping can't break it). No match → honest "dead", never confident-wrong.
- **"Active elsewhere"**: a transcript being written with no visible tmux host (cmux,
  bare terminal) shows working — and Enter refuses to resume it, because resuming an
  active conversation doubles the CLI.
- **Global keys via a context file**: the app mirrors the selection into
  `context.json`; tmux root bindings call `cagents-ctx shell|diff` which reads it. So
  C-s (split shell in the worktree) and C-d (diff-vs-master popup) work from inside
  the session. Accepted collisions, chosen by Samir: ← (layout) and C-d (Claude's
  quit-on-empty-prompt).
- **Deleted for leanness** (explicit audit): todos + pause/wake scripts + per-todo
  worktrees, plugins + meta session, peek, lazygit key, split-shell list key,
  ctrl+\\ and alt navigation, `=`, manual monitoring. Keys re-lettered: d done,
  f fork, h handoff, w waiting.
- **New states**: MONITORING / BACKGROUND from verified fire-and-forget ack
  signatures ("Monitor started (task…", "running in background with ID"), reset by
  the next human message; WAITING_EXTERNAL (w) tied to a PR, gh-polled (no push API
  exists): comments → re-alert "github comments", merge → done "merged".
- Orphan containers (pane 0 not cagents) are killed and rebuilt, never reattached.
