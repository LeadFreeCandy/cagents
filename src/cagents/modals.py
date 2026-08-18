"""Modal screens: small, fast, keyboard-first. Escape always cancels."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .claude_data import DiscoveredSession
from .format import human_age


class InputModal(ModalScreen[str | None]):
    """Single-line text input. Dismisses with the string, or None on escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    InputModal { align: center middle; }
    InputModal > Vertical {
        width: 70; max-width: 90%; height: auto;
        border: round $primary; background: $surface; padding: 1 2;
    }
    InputModal Label { margin-bottom: 1; text-style: bold; }
    """

    def __init__(self, title: str, initial: str = "", placeholder: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text)
            yield Input(value=self.initial, placeholder=self.placeholder)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewSessionModal(ModalScreen["tuple[str, str] | None"]):
    """Ask for a directory (and optional label) for a brand-new session.

    Deliberately *not* a task-description form (spec §7): you talk to
    Claude directly once attached.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "app.focus_next", "Next field", show=False),
        Binding("shift+tab", "app.focus_previous", "Prev field", show=False),
    ]

    DEFAULT_CSS = """
    NewSessionModal { align: center middle; }
    NewSessionModal > Vertical {
        width: 80; max-width: 95%; height: auto;
        border: round $primary; background: $surface; padding: 1 2;
    }
    NewSessionModal Label { text-style: bold; }
    NewSessionModal .hint { color: $text-muted; text-style: none; margin-bottom: 1; }
    NewSessionModal Input { margin-bottom: 1; }
    """

    def __init__(self, initial_dir: str) -> None:
        super().__init__()
        self.initial_dir = initial_dir

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Start a new Claude session")
            yield Static("Enter to start — you'll be talking to Claude directly.", classes="hint")
            yield Input(value=self.initial_dir, placeholder="project directory", id="dir")
            yield Input(placeholder="optional label (for you, not Claude)", id="label")

    def on_mount(self) -> None:
        self.query_one("#dir", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        directory = self.query_one("#dir", Input).value.strip()
        label = self.query_one("#label", Input).value.strip()
        self._finish(directory, label)

    def _finish(self, directory: str, label: str) -> None:
        directory = str(Path(directory).expanduser()) if directory else ""
        if not directory or not Path(directory).is_dir():
            self.query_one(".hint", Static).update(
                f"[red]Not a directory: {directory or '(empty)'}[/red]"
            )
            self.query_one("#dir", Input).focus()
            return
        self.dismiss((directory, label))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TrackModal(ModalScreen[str | None]):
    """Pick an existing Claude session (from Claude's own store) to track.

    Dismisses with the chosen session id.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "app.focus_next", "Next", show=False),
        Binding("shift+tab", "app.focus_previous", "Prev", show=False),
    ]

    DEFAULT_CSS = """
    TrackModal { align: center middle; }
    TrackModal > Vertical {
        width: 100; max-width: 95%; height: 80%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    TrackModal Label { text-style: bold; }
    TrackModal .hint { color: $text-muted; margin-bottom: 1; }
    TrackModal Input { margin-bottom: 1; }
    TrackModal OptionList { height: 1fr; }
    """

    def __init__(self, candidates: list[tuple[DiscoveredSession, str]]) -> None:
        """candidates: (discovered session, display title) pairs, newest first."""
        super().__init__()
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Track an existing session ({len(self.candidates)} found)")
            yield Static("Type to filter · Enter to track · Esc to cancel", classes="hint")
            yield Input(placeholder="filter…", id="filter")
            yield OptionList(id="candidates")

    def on_mount(self) -> None:
        self._refill("")
        self.query_one("#filter", Input).focus()

    def _refill(self, needle: str) -> None:
        from rich.text import Text

        option_list = self.query_one("#candidates", OptionList)
        option_list.clear_options()
        needle = needle.lower()
        now = datetime.now(timezone.utc)
        shown = 0
        for discovered, title in self.candidates:
            haystack = f"{title} {discovered.encoded_project}".lower()
            if needle and needle not in haystack:
                continue
            age = human_age(datetime.fromtimestamp(discovered.mtime, tz=timezone.utc), now)
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f"{title[:56]:<56} ", style="bold")
            row.append(f"{age:>4} ", style="dim")
            row.append(discovered.encoded_project, style="dim cyan")
            option_list.add_option(Option(row, id=discovered.session_id))
            shown += 1
            if shown >= 200:
                break
        if shown:
            option_list.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#candidates", OptionList).focus()

    def on_key(self, event) -> None:
        # j/k pass through to the list only when the filter isn't focused.
        if event.key in ("down", "up") and self.query_one("#filter", Input).has_focus:
            self.query_one("#candidates", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "no", "No"),
        Binding("n", "no", "No"),
        Binding("y", "yes", "Yes"),
    ]

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal > Vertical {
        width: 60; max-width: 90%; height: auto;
        border: round $warning; background: $surface; padding: 1 2;
    }
    ConfirmModal .question { text-style: bold; margin-bottom: 1; }
    ConfirmModal .keys { color: $text-muted; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.question, classes="question")
            yield Static("y — yes    n / esc — no", classes="keys")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


HELP_TEXT = """\
[bold]cagents — keys[/bold]

[bold cyan]Views[/bold cyan]
  1 / 2 / 3 / 4 grouped · queue · kanban · todos
  tab           next view

[bold cyan]Navigate[/bold cyan]
  j / k, ↑ / ↓  move
  h / l, ← / →  kanban: change column
  g / G         first / last

[bold cyan]Act on a session[/bold cyan]
  enter         attach (the real Claude CLI; detach: ctrl-b d)
  F             fork — new session from this one, prompt typed by you
  H             handoff — old session writes a spec, new one starts on it,
                old is marked done (r restores)
  *             related — visit this session's forks/handoffs/parent
  space         peek — read the transcript without attaching
  D             diff — review changes, comment, send comments to Claude
  V             rich diff — lazygit (per-commit + total, PR-style)
  t             shell — split terminal in the worktree/project
  o             open the newest recorded link (PR, artifact)
  r             mark reviewed / unmark
  m             monitoring — seen it, keep watching; re-alerts on activity
  e             edit note
  L             edit label
  x             untrack (never deletes Claude's data)

[bold cyan]Sessions[/bold cyan]
  n             start a new session
  a             track an existing session
  R             refresh now

[bold cyan]Todos (view 4)[/bold cyan]
  A             add todo        n   new session for the todo
  W             grow a git worktree + session for the todo
  d             done (offers to archive its workspaces) / reopen
  p             pause / unpause — timer (2d), wake condition, or indefinite
  x             delete todo     enter  attach to its newest session

[bold cyan]Fleet assistant & plugins[/bold cyan]
  :             ask in plain English (proposes a plan; you confirm)
  +             add plugin — the "meta" session writes a new keybind or
                automation into ~/.local/share/cagents/plugins (hot-loaded)

  ,             settings (sidebar rail · notifications · left-arrow capture)
  ?             this help · q quit\
"""


class HelpModal(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]

    DEFAULT_CSS = """
    HelpModal { align: center middle; }
    HelpModal > Vertical {
        width: 64; max-width: 90%; height: auto;
        border: round $primary; background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(HELP_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


class PaletteModal(ModalScreen[str | None]):
    """The fleet palette input. Clearly labeled as the AI-assisted surface
    (spec §10): everything else in cagents is deterministic; this one line
    is where you talk to the assistant about your *fleet*, not your code."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    PaletteModal { align: center top; }
    PaletteModal > Vertical {
        width: 90; max-width: 95%; height: auto; margin-top: 2;
        border: round $accent; background: $surface; padding: 1 2;
    }
    PaletteModal Label { text-style: bold; }
    PaletteModal .hint { color: $text-muted; margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(": fleet assistant")
            yield Static(
                "Plain English; proposes changes to cagents' bookkeeping only "
                "(review/notes/labels/tracking). You confirm before anything applies.",
                classes="hint",
            )
            yield Input(placeholder="e.g. mark everything in dealpilot reviewed — it's merged")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss(text or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanConfirmModal(ModalScreen[bool]):
    """Show the assistant's proposed plan; nothing applies without a yes."""

    BINDINGS = [
        Binding("escape", "no", "No"),
        Binding("n", "no", "No"),
        Binding("y", "yes", "Apply"),
    ]

    DEFAULT_CSS = """
    PlanConfirmModal { align: center middle; }
    PlanConfirmModal > Vertical {
        width: 100; max-width: 95%; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    PlanConfirmModal .reply { margin-bottom: 1; }
    PlanConfirmModal .keys { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, plan, titles: dict[str, str]) -> None:
        super().__init__()
        self.plan = plan
        self.titles = titles  # session_id -> display title

    def compose(self) -> ComposeResult:
        from rich.text import Text

        body = Text()
        for act in self.plan.actions:
            title = self.titles.get(act.session_id, act.session_id[:8])
            body.append("  → ", style="bold green")
            body.append(f"{act.action.replace('_', ' ')}", style="bold")
            if act.value:
                body.append(f' "{act.value}"')
            body.append(f"  {title}\n", style="cyan")
            if act.reason:
                body.append(f"      {act.reason}\n", style="dim italic")
        if not self.plan.actions:
            body.append("  (no actions proposed)\n", style="dim")
        for drop in self.plan.dropped:
            body.append("  ✗ refused: ", style="red")
            body.append(f"{drop}\n", style="dim")

        with Vertical():
            yield Label("Proposed plan")
            yield Static(self.plan.reply or "", classes="reply")
            yield Static(body)
            keys = "y — apply    n / esc — cancel" if self.plan.actions else "esc — close"
            yield Static(keys, classes="keys")

    def action_yes(self) -> None:
        self.dismiss(bool(self.plan.actions))

    def action_no(self) -> None:
        self.dismiss(False)


class TodoModal(ModalScreen["tuple[str, str] | None"]):
    """New todo: what, and (optionally) which project it belongs to."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "app.focus_next", "Next field", show=False),
        Binding("shift+tab", "app.focus_previous", "Prev field", show=False),
    ]

    DEFAULT_CSS = """
    TodoModal { align: center middle; }
    TodoModal > Vertical {
        width: 80; max-width: 95%; height: auto;
        border: round $primary; background: $surface; padding: 1 2;
    }
    TodoModal Label { text-style: bold; }
    TodoModal .hint { color: $text-muted; margin-bottom: 1; }
    TodoModal Input { margin-bottom: 1; }
    """

    def __init__(self, initial_dir: str = "") -> None:
        super().__init__()
        self.initial_dir = initial_dir

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("New todo")
            yield Static("Sessions and worktrees can be spawned from it later.", classes="hint")
            yield Input(placeholder="what needs doing?", id="todo-text")
            yield Input(value=self.initial_dir, placeholder="project directory (optional)", id="todo-dir")

    def on_mount(self) -> None:
        self.query_one("#todo-text", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = self.query_one("#todo-text", Input).value.strip()
        directory = self.query_one("#todo-dir", Input).value.strip()
        if not text:
            self.query_one("#todo-text", Input).focus()
            return
        if directory:
            directory = str(Path(directory).expanduser())
            if not Path(directory).is_dir():
                self.query_one(".hint", Static).update(f"[red]Not a directory: {directory}[/red]")
                self.query_one("#todo-dir", Input).focus()
                return
        self.dismiss((text, directory))

    def action_cancel(self) -> None:
        self.dismiss(None)


SETTINGS_META: list[tuple[str, str, str]] = [
    (
        "sidebar",
        "Sidebar rail",
        "Enter opens sessions in a side pane and the list stays as a left rail. "
        "Off: attaching takes the whole terminal (come back with ctrl-b d).",
    ),
    (
        "notifications",
        "Toast notifications",
        "Bottom-right popups for routine events. Errors and warnings always show.",
    ),
    (
        "capture_left",
        "Left arrow returns to list",
        "In the container: Left inside a session closes its pane (the session keeps "
        "running in the background). Trade-off: Left no longer moves the cursor while "
        "editing text in the Claude prompt.",
    ),
    (
        "desktop_notifications",
        "Desktop notifications",
        "macOS notification when a session starts needing you. With terminal-notifier "
        "installed, clicking it selects the task in the list.",
    ),
    (
        "auto_pause_days",
        "Auto-pause idle todos (days)",
        "Open todos with no session activity for this many days pause themselves. "
        "0 disables. Enter to type a new number.",
    ),
]


class SettingsModal(ModalScreen[None]):
    """`,` — toggles, applied immediately and persisted to the store."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("comma", "close", "Close"),
    ]

    DEFAULT_CSS = """
    SettingsModal { align: center middle; }
    SettingsModal > Vertical {
        width: 84; max-width: 95%; height: auto;
        border: round $primary; background: $surface; padding: 1 2;
    }
    SettingsModal Label { text-style: bold; }
    SettingsModal .hint { color: $text-muted; margin-bottom: 1; }
    SettingsModal #setting-desc { color: $text-muted; margin-top: 1; min-height: 3; }
    """

    def __init__(self, store, on_change) -> None:
        """on_change(key: str, value: bool) fires after each toggle."""
        super().__init__()
        self.store = store
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Settings")
            yield Static("enter — toggle · esc — close", classes="hint")
            yield OptionList(id="settings-list")
            yield Static(id="setting-desc")

    def on_mount(self) -> None:
        self._refill()
        self.query_one("#settings-list", OptionList).focus()

    def _refill(self, keep: str | None = None) -> None:
        from rich.text import Text

        option_list = self.query_one("#settings-list", OptionList)
        option_list.clear_options()
        for i, (key, label, _desc) in enumerate(SETTINGS_META):
            value = self.store.get_setting(key)
            row = Text()
            if isinstance(value, bool):
                row.append(" ▣ " if value else " □ ", style="bold green" if value else "dim")
                row.append(f"{label:<34}", style="bold" if value else "")
                row.append("on" if value else "off", style="green" if value else "dim")
            else:
                row.append(" # ", style="bold cyan")
                row.append(f"{label:<34}", style="bold")
                row.append(str(value), style="cyan")
            option_list.add_option(Option(row, id=key))
            if keep == key:
                option_list.highlighted = i
        if option_list.highlighted is None and option_list.option_count:
            option_list.highlighted = 0

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        for key, _label, desc in SETTINGS_META:
            if key == event.option.id:
                self.query_one("#setting-desc", Static).update(desc)
                return

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = event.option.id
        if key is None:
            return
        current = self.store.get_setting(key)
        if isinstance(current, bool):
            value = not current
            self.store.set_setting(key, value)
            self._refill(keep=key)
            self.on_change(key, value)
            return
        # numeric: type a new value
        self.app.push_screen(
            InputModal(f"New value for '{key}'", initial=str(current)),
            lambda text, k=key: self._numeric_entered(k, text),
        )

    def _numeric_entered(self, key: str, text: str | None) -> None:
        if text is None:
            return
        try:
            value = max(0, int(float(text.strip())))
        except ValueError:
            self.app.notify(f"Not a number: {text}", severity="warning")
            return
        self.store.set_setting(key, value)
        self._refill(keep=key)
        self.on_change(key, value)

    def action_close(self) -> None:
        self.dismiss(None)


class PauseModal(ModalScreen["tuple[str, str] | None"]):
    """Pause a todo. Dismisses with (kind, value):
    ("timer", "<duration text>") | ("criteria", "<text>") | ("none", "")."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    PauseModal { align: center middle; }
    PauseModal > Vertical {
        width: 80; max-width: 95%; height: auto;
        border: round $primary; background: $surface; padding: 1 2;
    }
    PauseModal Label { text-style: bold; }
    PauseModal .hint { color: $text-muted; margin-bottom: 1; }
    """

    def __init__(self, todo_text: str) -> None:
        super().__init__()
        self.todo_text = todo_text

    def compose(self) -> ComposeResult:
        from cagents.wake import parse_duration  # noqa: F401 (documented behavior)

        with Vertical():
            yield Label(f"Pause: {self.todo_text[:60]}")
            yield Static(
                "A duration (30m, 4h, 2d, 1w) sets a wake timer. Anything else is a "
                "wake condition — Claude writes a check script (you approve it first). "
                "Empty pauses until you unpause.",
                classes="hint",
            )
            yield Input(placeholder="2d · or: when the PR gets an approving review · or empty")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        from cagents.wake import parse_duration

        text = event.value.strip()
        if not text:
            self.dismiss(("none", ""))
        elif parse_duration(text) is not None:
            self.dismiss(("timer", text))
        else:
            self.dismiss(("criteria", text))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ScriptConfirmModal(ModalScreen[bool]):
    """Show a Claude-written wake script before it's saved. Nothing runs
    until a human has read it and said yes."""

    BINDINGS = [
        Binding("escape", "no", "No"),
        Binding("n", "no", "No"),
        Binding("y", "yes", "Approve"),
    ]

    DEFAULT_CSS = """
    ScriptConfirmModal { align: center middle; }
    ScriptConfirmModal > Vertical {
        width: 90; max-width: 95%; height: auto; max-height: 80%;
        border: round $warning; background: $surface; padding: 1 2;
    }
    ScriptConfirmModal .head { text-style: bold; margin-bottom: 1; }
    ScriptConfirmModal .keys { color: $text-muted; margin-top: 1; }
    ScriptConfirmModal #script-body { max-height: 20; overflow-y: auto; }
    """

    def __init__(self, criteria: str, script: str) -> None:
        super().__init__()
        self.criteria = criteria
        self.script = script

    def compose(self) -> ComposeResult:
        from rich.syntax import Syntax

        with Vertical():
            yield Static(f"Wake check for: {self.criteria}", classes="head")
            yield Static(
                Syntax(self.script, "bash", background_color="default"), id="script-body"
            )
            yield Static(
                "Runs every ~5 min, read-only, 30s timeout; exit 0 wakes the todo.\n"
                "y — approve and pause    n / esc — pause without the script",
                classes="keys",
            )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class RelatedModal(ModalScreen[str | None]):
    """Lineage browser (*): parent, siblings, children of a session.
    Dismisses with the chosen session id."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    RelatedModal { align: center middle; }
    RelatedModal > Vertical {
        width: 84; max-width: 95%; height: auto; max-height: 70%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    RelatedModal Label { text-style: bold; }
    RelatedModal .hint { color: $text-muted; margin-bottom: 1; }
    """

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        """rows: (session_id, kind label, display title)."""
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        from rich.text import Text

        with Vertical():
            yield Label("Related sessions")
            yield Static("enter — go to it · esc — close", classes="hint")
            option_list = OptionList(id="related-list")
            yield option_list

    def on_mount(self) -> None:
        from rich.text import Text

        option_list = self.query_one("#related-list", OptionList)
        for session_id, kind, title in self.rows:
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f" {kind:<18}", style="magenta")
            row.append(f"{title[:52]:<52} ", style="bold")
            row.append(session_id[:8], style="dim")
            option_list.add_option(Option(row, id=session_id))
        if self.rows:
            option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
