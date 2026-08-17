"""The diff screen: everything a worktree changed, pretty, commentable.

Review flow it enables (spec §6's "needs review" made concrete):
- open the diff for a session's worktree (committed + uncommitted vs the
  default branch, untracked files included);
- move a real cursor through it, leave comments anchored to file:line;
- pull the PR's review comments from GitHub (`gh`) into the same view;
- send the lot to the session's Claude in one keystroke — the screen
  composes the message, the app delivers it into the *real* CLI via tmux.

cagents never acts on the comments itself; Claude does, in its own session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .gitops import DiffLine, ReviewComment, WorktreeDiff

MAX_RENDER_LINES = 6000

_KIND_STYLE = {
    "add": ("green", "+"),
    "del": ("red", "-"),
    "ctx": ("", " "),
    "meta": ("dim italic", " "),
}


@dataclass
class DiffResult:
    """What the screen hands back to the app on dismiss."""

    send_message: str = ""  # non-empty -> deliver to the session's Claude
    comment_count: int = 0


@dataclass
class _Row:
    line: DiffLine
    index: int  # index into diff.lines, stable comment anchor


def _render_line(line: DiffLine, width: int) -> Text:
    if line.kind == "file":
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("▍", style="bold blue")
        text.append(f" {line.file} ", style="bold reverse")
        return text
    if line.kind == "hunk":
        return Text(line.text, style="cyan", no_wrap=True, overflow="ellipsis")
    style, sign = _KIND_STYLE.get(line.kind, ("", " "))
    text = Text(no_wrap=True, overflow="ellipsis")
    lineno = line.new_lineno or line.old_lineno
    text.append(f"{lineno or '':>5} ", style="dim")
    text.append(sign, style=style or "dim")
    text.append(" ")
    text.append(line.text[: width * 2], style=style)
    return text


def _render_comment(comment: ReviewComment) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("      ▌ ", style="yellow")
    author = "you" if comment.source == "local" else comment.author
    text.append(f"{author}: ", style="bold yellow")
    text.append(comment.body.replace("\n", " ")[:300], style="yellow")
    return text


def compose_review_message(
    diff: WorktreeDiff, comments: list[ReviewComment]
) -> str:
    """The message that gets pasted into the session's Claude prompt."""
    header = (
        f"I reviewed your diff in {diff.directory} "
        f"({diff.branch or 'HEAD'} vs {diff.base}). "
        f"Please address these review comments:"
    )
    lines = [header, ""]
    n = 0
    for comment in comments:
        n += 1
        where = f"{comment.file}:{comment.line}" if comment.file else "(overall)"
        who = "" if comment.source == "local" else f" [from {comment.author} on GitHub]"
        lines.append(f"{n}. {where}{who} — {comment.body}")
    return "\n".join(lines)


class DiffScreen(ModalScreen[DiffResult | None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("n", "next_file", "Next file"),
        Binding("p", "prev_file", "Prev file", show=False),
        Binding("c", "comment", "Comment"),
        Binding("g", "pull_github", "GitHub comments"),
        Binding("s", "send", "Send to Claude"),
    ]

    DEFAULT_CSS = """
    DiffScreen { align: center middle; }
    DiffScreen > Vertical {
        width: 96%; height: 94%;
        border: round $primary; background: $surface;
    }
    DiffScreen #diff-title { height: 1; padding: 0 1; background: $panel; }
    DiffScreen #diff-list { height: 1fr; }
    DiffScreen #diff-status { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, diff: WorktreeDiff, target_desc: str, github_puller=None) -> None:
        """github_puller: () -> list[ReviewComment]; injectable for tests."""
        super().__init__()
        self.diff = diff
        self.target_desc = target_desc  # e.g. "session 'Fix auth'"
        self.github_puller = github_puller
        # comment anchor: index into diff.lines -> comments there
        self.comments: dict[int, list[ReviewComment]] = {}
        self.overall_comments: list[ReviewComment] = []

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        title = Text(no_wrap=True, overflow="ellipsis")
        title.append("Δ ", style="bold yellow")
        title.append(f"{self.diff.branch or 'HEAD'}", style="bold")
        title.append(f" vs {self.diff.base}  ", style="dim")
        title.append(f"{len(self.diff.files)} files ", style="bold")
        title.append(f"+{self.diff.additions} ", style="green")
        title.append(f"−{self.diff.deletions}  ", style="red")
        title.append(self.diff.directory, style="dim cyan")
        with Vertical():
            yield Static(title, id="diff-title")
            yield OptionList(id="diff-list")
            yield Static(id="diff-status")

    def on_mount(self) -> None:
        self._rebuild(keep_index=0)
        self.query_one("#diff-list", OptionList).focus()
        self._status()

    # -- rendering --------------------------------------------------------------

    def _rebuild(self, keep_index: int | None = None) -> None:
        option_list = self.query_one("#diff-list", OptionList)
        if keep_index is None:
            keep_index = option_list.highlighted or 0
        width = max(60, self.app.size.width - 10)
        options: list[Option] = []
        if self.diff.empty:
            options.append(Option("  Nothing changed in this worktree.", disabled=True))
        for comment in self.overall_comments:
            options.append(Option(_render_comment(comment), disabled=True))
        shown = self.diff.lines[:MAX_RENDER_LINES]
        for i, line in enumerate(shown):
            selectable = line.kind in ("add", "del", "ctx")
            options.append(
                Option(_render_line(line, width), id=str(i), disabled=not selectable)
            )
            for comment in self.comments.get(i, ()):
                options.append(Option(_render_comment(comment), disabled=True))
        if len(self.diff.lines) > MAX_RENDER_LINES:
            options.append(
                Option(
                    f"  … {len(self.diff.lines) - MAX_RENDER_LINES} more lines not shown",
                    disabled=True,
                )
            )
        option_list.clear_options()
        option_list.add_options(options)
        if options:
            index = min(keep_index, len(options) - 1)
            if not options[index].disabled or any(not o.disabled for o in options):
                while index < len(options) and options[index].disabled:
                    index += 1
                if index >= len(options):
                    index = next((i for i, o in enumerate(options) if not o.disabled), 0)
                option_list.highlighted = index

    def _status(self, extra: str = "") -> None:
        total = sum(len(v) for v in self.comments.values()) + len(self.overall_comments)
        base = (
            f"{total} comment{'s' if total != 1 else ''} · c comment · g pull GitHub · "
            f"s send to {self.target_desc} · n/p files · esc close"
        )
        self.query_one("#diff-status", Static).update(extra or base)

    # -- cursor helpers -----------------------------------------------------------

    def _current_line_index(self) -> int | None:
        option_list = self.query_one("#diff-list", OptionList)
        if option_list.highlighted is None:
            return None
        option = option_list.get_option_at_index(option_list.highlighted)
        if option.id is None:
            return None
        try:
            return int(option.id)
        except ValueError:
            return None

    def action_cursor_down(self) -> None:
        self.query_one("#diff-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#diff-list", OptionList).action_cursor_up()

    def _jump_file(self, delta: int) -> None:
        option_list = self.query_one("#diff-list", OptionList)
        current = self._current_line_index()
        file_indices = [i for i, l in enumerate(self.diff.lines) if l.kind == "file"]
        if not file_indices:
            return
        if current is None:
            current = 0
        if delta > 0:
            target = next((i for i in file_indices if i > current), file_indices[0])
        else:
            candidates = [i for i in file_indices if i < current - 1]
            target = candidates[-1] if candidates else file_indices[-1]
        # find the option holding id=str(first selectable line after target)
        for option_index in range(option_list.option_count):
            option = option_list.get_option_at_index(option_index)
            if option.id is not None and int(option.id) > target and not option.disabled:
                option_list.highlighted = option_index
                return

    def action_next_file(self) -> None:
        self._jump_file(+1)

    def action_prev_file(self) -> None:
        self._jump_file(-1)

    # -- commenting -----------------------------------------------------------------

    def action_comment(self) -> None:
        index = self._current_line_index()
        if index is None:
            self._status("Put the cursor on a diff line first.")
            return
        line = self.diff.lines[index]
        from .modals import InputModal

        where = f"{line.file}:{line.new_lineno or line.old_lineno}"
        self.app.push_screen(
            InputModal(f"Comment on {where}", placeholder="what should Claude change?"),
            lambda text: self._comment_added(index, text),
        )

    def _comment_added(self, index: int, text: str | None) -> None:
        if not text or not text.strip():
            return
        line = self.diff.lines[index]
        self.comments.setdefault(index, []).append(
            ReviewComment(
                file=line.file,
                line=line.new_lineno or line.old_lineno,
                body=text.strip(),
                author="you",
                source="local",
            )
        )
        self._rebuild()
        self._status()

    # -- GitHub -----------------------------------------------------------------------

    def action_pull_github(self) -> None:
        if self.github_puller is None:
            self._status("GitHub pulling not available here.")
            return
        self._status("Pulling PR comments from GitHub…")
        self._pull_github_worker()

    def _pull_github_worker(self) -> None:
        self.run_worker(self._do_pull_github, thread=True, exclusive=True, group="github")

    def _do_pull_github(self) -> None:
        try:
            comments = self.github_puller()
        except Exception as error:
            self.app.call_from_thread(self._status, f"GitHub: {error}")
            return
        self.app.call_from_thread(self._github_pulled, comments)

    def _github_pulled(self, comments: list[ReviewComment]) -> None:
        # Anchor inline comments to matching diff lines; the rest go on top.
        by_location: dict[tuple[str, int], int] = {}
        for i, line in enumerate(self.diff.lines):
            if line.kind in ("add", "ctx") and line.new_lineno:
                by_location.setdefault((line.file, line.new_lineno), i)
        added = 0
        for comment in comments:
            if any(
                c.body == comment.body and c.author == comment.author
                for bucket in self.comments.values()
                for c in bucket
            ) or any(
                c.body == comment.body and c.author == comment.author
                for c in self.overall_comments
            ):
                continue  # already pulled
            anchor = by_location.get((comment.file, comment.line))
            if anchor is not None:
                self.comments.setdefault(anchor, []).append(comment)
            else:
                self.overall_comments.append(comment)
            added += 1
        self._rebuild()
        self._status(f"Pulled {added} new comment{'s' if added != 1 else ''} from GitHub.")

    # -- send / close --------------------------------------------------------------------

    def _all_comments(self) -> list[ReviewComment]:
        ordered: list[ReviewComment] = []
        for index in sorted(self.comments):
            ordered.extend(self.comments[index])
        return ordered + self.overall_comments

    def action_send(self) -> None:
        comments = self._all_comments()
        if not comments:
            self._status("No comments yet — press c on a line first.")
            return
        message = compose_review_message(self.diff, comments)
        self.dismiss(DiffResult(send_message=message, comment_count=len(comments)))

    def action_close(self) -> None:
        self.dismiss(None)
