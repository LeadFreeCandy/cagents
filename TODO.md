# Requested improvements

- [x] Handoff: choose the successor's provider (Claude or Codex) and model; preserve the source history and cross-provider lineage.
- [x] Provider icon: use a single terminal column after the state to distinguish Claude and Codex (`✳` Claude, `›` Codex).
- [x] Conversation title width: configurable in Settings (8–120 columns); default to 22 columns (previous maximum: 44).
- [ ] Recap line: optional indented recap beneath every conversation row, disabled by default. [Design proposal](RECAP_DESIGN.md) covers generation, caching, freshness, and memory use; setting and row expansion await design discussion.

The first three items are implemented. Recap line is a design discussion for now.

- [x] Reduce navigation flicker: update stable rows in place, debounce conversation switches, reuse viewer clients, and resize only when focus changes.
- [ ] Fix the remaining two-stage arrow resize and residual bar. [Investigation and interaction proposal](RESIZE_DESIGN.md): real Codex redraws around 80 ms after the layout moves; fixed-width focus removes that recurring second repaint. Residual bar still needs visual reproduction.
- [x] Fix new Codex panes exiting: persist initial context through the native API before resuming the new thread; verify startup and subsequent resume with the real CLI.
