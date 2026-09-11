# cagents3: native tmux panes

Available on `main`, developed on `feat/cagents3-direct-panes`. The `cagents`
launcher remains available. cagents3 has its own tmux server and bookkeeping file.

## Transport

One tmux server owns the dashboard, agent panes, and terminal tabs. Selecting a
conversation swaps its existing pane into the session tab. Unselected panes
remain in hidden home windows; their process, terminal state, and draft survive.
There is no terminal emulator, output renderer, PTY relay, or nested tmux client
between the managed agent and the user's tmux client.

The queue is one Textual pane, shared between native tab windows by swapping it
with a left-hand placeholder on tab selection. Focus changes do not resize the
agent; explicit size controls still do. Native tmux handles scrolling, selection,
paste, colors, and the tab bar.

Existing conversations hosted on another server retain one attachment hop.
cagents3 does not migrate or automatically suspend those processes. New sessions
and sessions resumed locally use the direct path.

## Preserved behavior and verification

- [x] Stable pane identity through selection, tabs, reload, and shutdown.
- [x] Native input, mouse wheel, clipboard, paste, colors, and Ctrl-G.
- [x] Persistent per-conversation terminals, extra terminals, and diff tab.
- [x] Accurate registry/state polling after panes move; identity-checked restart
      and idle suspension, with terminal siblings preserved.
- [x] New Claude/Codex conversations, resume, fork, handoff, and terminal shims.
- [x] Existing queue views, search, labels, provider icons, settings, auto-done,
      waiting/PR/Jira, notifications, and undo remain on the existing app logic.
- [x] Separate `cagents3` launcher and state; original launcher retained.
- [x] Hover wakes idle conversations without selecting them, switching the
      visible pane, or interrupting its attachment. Applied to both versions.
- [x] Full suite and real tmux/native CLI QA; document any remaining limitations.

The starting baseline has 559 passing tests and 3 skipped tests. Legacy terminal
tests remain because the original backend remains available. New transport tests
are first run against the old backend to establish the behavioral difference,
then wired to the direct backend without weakening their assertions.

## Trying it

Run `cagents3`. Both launchers use the main checkout's virtual environment;
the feature worktree is no longer required to run cagents3.

The default server is `cagents3`; state lives at
`~/.local/share/cagents3/state.json` (respecting `XDG_DATA_HOME`). On first launch,
only bookkeeping and settings are copied from the original state file. Later
changes to the two files are independent. Agent transcripts remain in their
native Claude/Codex directories. Opening an existing conversation attaches to
its existing process when available; explicit restart acts on that conversation.
Automatic idle suspension applies only to agents hosted by cagents3.

The native tabs are session, diff, term-1, additional terminals, and +term. Ctrl-T
toggles the terminal; Ctrl-D opens the diff; Ctrl-G goes to the queue's first row.
Enter and mouse focus retain pane dimensions. Explicit arrows still provide
wide/compact/zoom sizes, and the existing arrow-capture settings remain available.
Quitting detaches the dashboard; relaunching preserves agents and terminal tabs.
Ctrl-R and `:restart` retain their existing conversation/lifecycle behavior.

A real resize still asks the native CLI to redraw. This change removes the extra
workspace/session relays and automatic focus resizes; it does not claim to remove
latency inside a CLI's own resize handling. A conversation on another tmux server
needs one ordinary attachment client. No terminal output is parsed or recreated
for display in either case.

## Regression evidence

The first four transport tests failed against the original layout: the displayed
pane was a relay, focus resized the queue, the queue was outside the tab windows,
and terminals were viewed through another workspace. Their pane/process/draft
assertions pass against the direct backend. The fixture's transport wiring was
changed; those assertions were retained.

Additional failing tests pinned down quit destroying the shared server, original
sessions being eligible for automatic suspension, lost zoom, duplicate Textual
mount handling, explicit size controls, terminal activity attribution, tab order,
reserved-name collisions, and lost provider/shim environments at spawn/restart.
All original regression tests are retained without changing their assertions.

The shared hover bug reproduced in both checkouts before the fix: queue, grouped,
and kanban hover selected another conversation, and background Done resumes could
replace or reattach the visible pane. Five regression cases failed in each
checkout. The fix separates hover activity from selection and limits viewer
changes to the selected conversation. Seven hover tests now cover these cases,
click/keyboard navigation, and selected-conversation resume. The existing idle
hover-to-wake regression remains. A native-pane integration test additionally
checks visible pane identity, process IDs, and drafts through hover/click/keyboard.

Full-suite results at the hover-fix milestone: original checkout 566 passed,
3 skipped; cagents3 598 passed, 5 skipped. The additional skips are the opt-in
installed-CLI checks described below, which were also run explicitly.

The first new-conversation QA only checked pane creation, focus, and persistence.
That missed an exited startup placeholder: a retained pane still has an ID and
can receive focus. Strengthened tests reproduced four failures by requiring new
shells and terminal tabs to execute typed input, including configured tmux shell
commands. New shells now explicitly start tmux's configured command or login
shell instead of respawning the placeholder.

`tests/dashboard_harness.py` drives the actual dashboard through a PTY, with no
app-method or provider-launch stubs. The existing launch test now shares this
harness and retains its pane/focus/relaunch coverage. Opt-in workflow tests type
`claude` and `codex` into the new shell, verify the native composer and tracked
identity, type an unsent Unicode draft, switch away and back through the queue,
execute commands in the conversation terminal, and quit/relaunch the dashboard.
They assert the original agent PID, draft, and terminal survive. Every run saves
pane inventories, ANSI terminal captures, and app logs under pytest's temporary
`dashboard-artifacts` directory; all QA tmux servers and provider homes are
isolated from live conversations. Navigation workflows leave drafts unsent;
scroll fixtures submit only to the refused localhost endpoint, never a model service.

Promotion QA after fixing shell startup: the complete suite with installed-CLI
QA enabled passed 604 tests, with 3 opt-in checks skipped. The separate macOS
clipboard run passed all 3 selected checks and restored the original pasteboard
contents and formats. The reusable dashboard harness was also run against an
isolated copy of the unfixed implementation and failed because typed shell input
was not executed. No existing regression test was removed or disabled.

Navigation also checks tmux's client message history, since yellow status-bar
errors are outside `capture-pane` output. Two dimming tests (enabled/disabled)
and the dashboard workflow reproduced `No such pane: :.1` before the fix: the
dimming hook ran in hidden windows containing only one pane. The hook now runs
only in UI tabs with a second pane. Tests retain dimming, focus, size, and PID
assertions; the shared harness checks messages during navigation and saves them
alongside its terminal captures.

Mouse-motion regressions also exercise Textual's all-motion reporting. The
copy-mode `Any` binding used for bracketed paste was cancelling history on
unbound mouse movement, so merely moving the pointer reset Codex to the bottom.
Both emacs and vi tests failed before the fix, as did the real dashboard and
installed Codex. The fallback now forwards mouse events with native tmux handling
and leaves the paste path unchanged. Tests cover scroll position and selection
across the content, rail, border, and status line; they retain focus, draft, and
paste assertions. The earlier sleeping-rail fixture did not request all mouse
motion and therefore missed this case. QA also cleans up its isolated Codex
daemon if graceful shutdown stalls, verifying the temporary executable path and
process birth stamp before sending signals.
Verification for this fix: all six new regression cases fail against the original
input binding. The complete suite with installed-CLI QA enabled passes 612 tests
(3 opt-in checks skipped); the separate macOS clipboard run passes all 3 selected
checks and restores the original pasteboard contents and formats.

Real PTY tests exercise the dashboard's new-conversation keys, Ctrl-G, quit and
relaunch, the original process identities, native mouse packets, selection,
bracketed paste, per-conversation terminals, and OSC color queries. Opt-in tests
also run the installed Codex 0.154.0 and Claude Code 2.1.268 with temporary config
and refused localhost API endpoints. Codex message backgrounds, scroll/copy,
multiline Unicode paste, and preserved drafts were checked. Native macOS clipboard
copy was checked under the clipboard-preserving harness. These are actual CLI/PTY
checks; GUI automation of iTerm was unavailable.

Run the default suite with `.venv/bin/python -m pytest -q`. Installed CLI QA:

```sh
CAGENTS_NATIVE_CLI_TESTS=1 .venv/bin/python -m pytest -q tests/live/test_direct_native_clis.py tests/live/test_dashboard_workflow.py
```

The macOS clipboard check additionally sets `CAGENTS_MAC_CLIPBOARD_TESTS=1` and
runs pytest under `tests/live/preserve_clipboard.swift`, preserving all existing
pasteboard formats. The default suite uses temporary copy destinations.
