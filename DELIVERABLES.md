# Deliverables

Every distinct feature request, stated requirement, strongly-implied requirement, and
bug-fix end-state Samir has asked for since pulling this repo down, in the order asked.
One bullet per distinct ask; sub-bullets where a single message bundled multiple
requirements together.

- Pull the `cagents` repo down from Samir's own GitHub and wire it up in place of the
  local, untracked copy that was already on disk.
- Install `cagents` onto `$PATH`, including the in-progress feature branch
  ("cagents-features") rather than just the stable `main` line.
- Investigate a "tmux hook" that might be causing an error, and turn it off if it's the
  cause. (Turned out to be an unrelated, already-inactive zsh alias — no action needed,
  but the diagnostic ask stands as stated.)
- Remove the spacebar-to-preview modal entirely; that screen real estate should
  instead be a persistent preview on the right side of the layout.
- The right-hand pane should render as close to how real Claude Code actually looks as
  possible — not a re-implemented/custom text summary.
- Root-cause and fix desktop/toast notifications appearing even after they were turned
  off in settings.
- The app must not be "completely broken" / unusable: sessions flashing the terminal,
  failing to open, and reporting "could not find session" needed to be actually
  reproduced (not just theorized about) and fixed at the root cause, not papered over.
- The right preview pane must not be black-screen/custom-formatted text — it must be
  the actual embedded conversation, indistinguishable from the real Claude Code UI.
- Explain "how do you mark something done" (the `r` / reviewed keybind).
- A newly created session must be genuinely reachable/usable immediately after
  creation, not just recorded in cagents' bookkeeping.
- An existing/previously-tracked session brought back into cagents must actually be
  functional when resumed.
- The hover/selection preview and pressing `enter` to attach must render with the
  *exact same formatting* — i.e. they must be the same real mechanism, not two
  different code paths that could ever disagree.
- Run a real end-to-end test that actually exercises the real software (not mocks),
  and fix whatever it turns up.
- Do not stop investigating until the specific reported bugs are actually reproduced,
  root-caused, and explained — including why prior fixes didn't seem to take effect.
- The preview pane must not show `did` / `needs` single-line summaries under a
  conversation unless that's turned on — and if that feature is kept, it must be
  gated behind a togglable setting rather than always-on.
- cagents must always default to the grouped list view (view `1`) on startup,
  regardless of whatever view was active in a prior run.
- Provide a way to fully reset cagents' own state and clear all of its tracked
  sessions when it gets into a broken/stuck state (scoped, on confirmation, to
  cagents' own bookkeeping — not the underlying real Claude Code transcripts).
- Fix the specific bug where the queue/list reports "needs you" for a session that
  isn't actually waiting on the user.
- Add a keybind for "done here, but waiting on team review" as a distinct, low-priority
  paused/external state:
  - it should automatically reopen (with a distinguishing status, e.g. "github
    comments") if the linked PR gets new comments;
  - it should automatically mark itself done ("done — merged") once the linked PR is
    merged;
  - prefer a push/subscribe mechanism over polling GitHub for this, if one is
    actually available via `gh`.
- Remove the old "monitoring" feature entirely (the manual "seen it, keep watching"
  flag/keybind) — it's no longer needed.
- The footer/keybind bar at the bottom should show more of the available keybinds,
  filling available width in a priority order rather than silently truncating; Fork
  and "mark done" (rename the "Reviewed" label to "Done") should always be visible
  whenever anything below Attach is visible; general opinionated cleanup of the
  keybind list was explicitly welcomed.
- Todos should be togglable via a setting, and the Todos keybind/footer entry should
  be hidden whenever that setting is off.
- Fix the sidecar layout bug where enabling the "sidebar" setting produced three
  panes (list, a stale internal fallback preview, and the real live pane) instead of
  the intended two (list, and whichever preview mechanism is actually active).
- When creating a new session, the directory field should default to wherever the
  user actually launched `cagents` from — not whatever session happens to be
  currently selected in the list.
- The new-session directory field should support typing a path and tab-completing
  through matching subdirectories.
- If technically feasible, provide an "elegant" terminal-passthrough option for
  picking the new-session directory: drop into a real interactive shell (so tools
  like `zoxide` work exactly as they do normally) and use whatever directory the user
  ends up in when they exit it.
- Confirm whether the "needs you" mis-reporting bug (see above) was actually fixed.
- Add a new, low-priority `Monitoring` session state: Claude has its own "monitor"
  running (a distinct Claude Code feature, not a background agent) and is idle,
  waiting on that monitor rather than genuinely waiting on the user.
- Add a new, low-priority `Background` session state: Claude is idle/waiting for
  input, but has a background command (or background agent) still running.
  `Monitoring` should rank as higher priority than `Background`.
- (Requested, explicitly deferred — not yet implemented) When marking a session
  "waiting on review" (`w`), cagents should check the session's worktree/branch and
  try to automatically find the associated PR for it. If none can be found
  automatically, it should prompt the user to paste in the PR's link or number
  instead of silently failing.
- Produce this file: a chronological markdown list of every distinct feature request
  and requirement made since pulling the repo down.
- Push this markdown file, and all accumulated local changes, up to the git repo.
