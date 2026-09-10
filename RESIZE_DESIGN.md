# Arrow navigation and resize investigation

Measured on September 10, 2026 with tmux 3.6a and Codex 0.154.0.

## Findings

The two-stage terminal output is reproducible. In an isolated 146×35 terminal,
the current arrow bindings repaint the layout within about 1–5 ms, then the
native Codex conversation clears and redraws about 78–86 ms after the keypress.
During rapid arrow presses, further repaints can arrive about 250 ms after the
last key. Earlier changes to list updates and viewer-client reuse do not remove
this resize sequence.

Three configurations were compared using the real Codex CLI, with a temporary
home, fake credentials, and no model prompts:

| Configuration | Observation |
| --- | --- |
| Current layout: outer split → workspace tabs → native session | Layout repaint followed by a conversation redraw around 80 ms later |
| Workspace layer removed | The same roughly 80 ms second redraw remains |
| Pane widths held fixed while focus changes | The recurring delayed resize repaint disappears; repeated focus of the same pane emits no output |

A synthetic terminal that redraws immediately receives spaced resizes in about
2–11 ms. With rapid arrows it can still be processing an older width about
170 ms after a subsequent keypress. tmux's repeated pane resize handling has a
[250 ms timer](https://github.com/tmux/tmux/blob/3.6a/server-client.c#L2650-L2718).
The native CLI's redraw behavior and tmux's resize queue both matter; reducing
Python calls or removing a nesting layer alone will not make resizing atomic.

## Recommended interaction

1. Keep the rail at a stable width when focus moves. Ordinary Left/Right moves
   focus between the list and conversation without reflowing either pane.
2. Retain Shift/Option-Left/Right for explicit width changes, and `i` for full
   conversation view. An explicit zoom should go directly to its destination,
   without first collapsing to the intermediate rail width.
3. Show focus with the active border and list selection. Recoloring the entire
   conversation on each focus change adds another large paint.

This changes the current bare-arrow sizing behavior, so it is a design proposal,
not an implemented default. If bare arrows must remain size controls, the native
CLI will still need to reflow. A visually atomic resize would require coordinated
presentation of the outer layout and completed inner frame; an arbitrary sleep
or extra forced redraw does not establish that the frame is ready.

## Remaining visual check

Computer Use explicitly denied access to iTerm. These measurements come from
isolated PTY output, real tmux servers, and terminal-buffer captures, not a GUI
recording. The rail in the timing harness is synthetic; the conversation is the
real Codex CLI. Tests used both spaced keys (700 ms apart) and rapid keys (80 ms
apart), following wide → narrow → full → narrow → wide.

The captured terminal buffers settle cleanly. The persistent leftover bar has
not been reproduced or fixed, and Claude's redraw timing has not been measured.
Identify whether the residual bar is the outer divider, workspace tab bar, or
native conversation content before adding a repaint workaround.

Local diagnostic harness: `/tmp/cagents-resize-probe.py`. It accepts `--codex`,
`--direct` (remove the workspace layer), and `--fixed` (focus without resizing).
It creates unique test servers and stops them and its temporary Codex daemon on
exit. Raw captures from this investigation are in `/tmp/cg-resize-d53labba`
(current layout), `/tmp/cg-resize-azc_fh2u` (direct), and
`/tmp/cg-resize-qdm_ki1j` (fixed width). No running user sessions were changed.
