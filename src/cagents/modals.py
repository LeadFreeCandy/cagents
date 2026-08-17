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
  1 / 2 / 3     grouped · queue · kanban
  tab           next view

[bold cyan]Navigate[/bold cyan]
  j / k, ↑ / ↓  move
  h / l, ← / →  kanban: change column
  g / G         first / last

[bold cyan]Act on a session[/bold cyan]
  enter         attach (the real Claude CLI; detach: ctrl-b d)
  r             mark reviewed / unmark
  e             edit note
  L             edit label
  x             untrack (never deletes Claude's data)

[bold cyan]Sessions[/bold cyan]
  n             start a new session
  a             track an existing session
  R             refresh now

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
