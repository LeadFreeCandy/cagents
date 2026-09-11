# Requested improvements

- [x] Handoff: choose the successor's provider (Claude or Codex) and model; preserve the source history and cross-provider lineage.
- [x] Provider icon: use a single terminal column after the state to distinguish Claude and Codex (`✳` Claude, `›` Codex).
- [x] Conversation title width: configurable in Settings (8–120 columns); default to 22 columns (previous maximum: 44).
- [ ] Recap line: optional indented recap beneath every conversation row, disabled by default. [Design proposal](RECAP_DESIGN.md) covers generation, caching, freshness, and memory use; setting and row expansion await design discussion.

The first three items are implemented. Recap line is a design discussion for now.

- [x] Reduce navigation flicker: update stable rows in place, debounce conversation switches, reuse viewer clients, and resize only when focus changes.
- [ ] Fix the remaining two-stage arrow resize and residual bar. [Investigation and interaction proposal](RESIZE_DESIGN.md): real Codex redraws around 80 ms after the layout moves; fixed-width focus removes that recurring second repaint. Residual bar still needs visual reproduction.
- [x] Fix new Codex panes exiting: persist initial context through the native API before resuming the new thread; verify startup and subsequent resume with the real CLI.
- [x] Fix Codex setup blocks becoming conversation titles/previews, and fit narrow list rows so provider icons and ages remain visible without double truncation.
- [x] Follow provider-maintained conversation names and native renames, including Codex names while suspended; isolate polling failures to the affected thread.
- [x] Preserve Codex's native message/composer backgrounds when starting detached by carrying the dashboard terminal's colors into the agent pane.
- [x] Remove the Codex scrolling adapter: use native mouse forwarding or terminal history, with consistent first-tick scrolling and no helper processes or synthetic CLI keys.
- [x] Audit false working states: honor native idle and Claude turn completion, invalidate stale runtime status, and exclude Codex scrollback from spinner detection; verify failing regressions, native sessions, list transitions, scrolling, and clipboard behavior.
- [x] Ctrl+G returns from conversations and workspace tabs to the first queue conversation, including when chat is full width.
- [x] Keep drag selections highlighted after release and copy directly to the macOS clipboard; verify with the actual pasteboard and native Codex CLI while preserving drafts and the original clipboard.
- [x] Restore system clipboard copying through nested tmux and preserve bracketed paste when returning from scrollback; verify drag-copy, multiline paste, focus and scrolling with real terminal events.
