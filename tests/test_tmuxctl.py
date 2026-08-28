"""pane_shows_working / pane_shell_count: live-status detection that must
only ever look at the tail of the pane, never the whole scrollback."""

from __future__ import annotations

from unittest.mock import patch

from cagents.tmuxctl import TmuxClient, pane_shell_count, pane_shows_working


def test_capture_pane_rejoins_soft_wrapped_lines():
    # Without -J, a resized/narrower terminal can split a single status
    # line ("Baking… (45s · stats)") across two physical lines at an
    # arbitrary point — every pane-text pattern here assumes one line.
    with patch("subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        TmuxClient().capture_pane("alpha")
    args = run.call_args[0][0]
    assert "-J" in args


def test_working_marker_in_scrollback_alone_is_not_working():
    # A real bug: a user's old question ("what is still running?")
    # containing the exact marker text, sitting well above the live
    # footer, must not be read as a live spinner.
    pane = (
        "❯ what is still running?\n\n"
        "⏺ Nothing important.\n\n"
        "────────────\n"
        "❯ \n"
        "────────────\n"
        "  Sonnet 5 | ctx: 12%\n"
        "  ⏵⏵ auto mode on · ← 1 agent"
    )
    assert pane_shows_working(pane) is False


def test_working_marker_as_the_actual_last_line_is_working():
    assert pane_shows_working("✻ Simmering… (esc to interrupt)") is True
    assert pane_shows_working("some old text\n✻ Brewed for 4s · 1 shell still running") is True


def test_monitors_still_running_footer_is_not_working():
    # Real bug: a worktree-creation session sitting idle with only
    # Monitors going ("✻ Churned for 7m 6s · 2 monitors still running")
    # showed as WORKING in the list — the generic "still running" marker
    # matched this idle footer the same as the mid-turn shell spinner.
    # Monitors persist past the end of the turn by design; this must fall
    # through to MONITORING, not read as an active turn.
    assert pane_shows_working("✻ Churned for 7m 6s · 2 monitors still running") is False
    assert pane_shows_working("✻ Churned for 7m 6s · 1 monitor still running") is False


def test_newer_spinner_format_without_esc_to_interrupt_is_working():
    # Replicated live (v2.1.236+): the spinner line dropped "(esc to
    # interrupt)" for elapsed-time/token stats instead — no
    # _WORKING_MARKERS text at all. Only the "Verb… (" shape distinguishes
    # it from the past-tense, already-finished form ("Baked for 12m 53s").
    assert pane_shows_working("✽ Baking… (45s · thinking some more with medium effort)") is True
    assert pane_shows_working("· Baking… (2m 34s · ↓ 10.0k tokens)") is True


def test_newer_spinner_can_sit_above_an_idle_looking_footer():
    # An input box + footer can render below the spinner line while
    # Claude is still actively streaming — "last line" alone would miss
    # this, hence the wider structural-pattern tail window.
    pane = (
        "some earlier turn text\n"
        "✽ Baking… (45s · thinking some more with medium effort)\n\n"
        "─────────────\n"
        "❯ \n"
        "─────────────\n"
        "  Sonnet 5 | ctx: 40%\n"
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent"
    )
    assert pane_shows_working(pane) is True


def test_finished_past_tense_spinner_is_not_working():
    # No ellipsis -> already done, sitting in scrollback as a historical
    # marker — must not be misread as still in progress.
    pane = (
        "✻ Baked for 12m 53s\n\n"
        "─────────────\n"
        "❯ \n"
        "─────────────\n"
        "  Sonnet 5 | ctx: 40%\n"
        "  ⏵⏵ auto mode on · ← 1 agent"
    )
    assert pane_shows_working(pane) is False


def test_ellipsis_spinner_still_respects_a_wide_enough_tail_window():
    # Sanity: a spinner-shaped line buried well outside the tail window
    # (e.g. deep, old scrollback) correctly does not count.
    padding = "\n".join(f"line {i}" for i in range(30))
    pane = f"✽ Baking… (45s · stats)\n{padding}\n  ⏵⏵ auto mode on · ← 1 agent"
    assert pane_shows_working(pane) is False


def test_shell_count_ignores_scrollback_mentions():
    pane = (
        "❯ how many shells are running right now?\n\n"
        "⏺ None.\n\n"
        "  Sonnet 5 | ctx: 12%\n"
        "  ⏵⏵ auto mode on · ← 1 agent"
    )
    assert pane_shell_count(pane) == 0


def test_shell_count_reads_the_live_footer():
    pane = "  Sonnet 5 | ctx: 12%\n  ⏵⏵ auto mode on · 1 shell running · ← 1 agent"
    assert pane_shell_count(pane) == 1
    pane2 = "✻ Brewed for 4s · 2 shells running"
    assert pane_shell_count(pane2) == 2


def test_shell_count_defers_to_the_mid_turn_still_running_marker():
    # "N shell(s) still running" is the mid-turn spinner's own phrasing
    # (pane_shows_working's job) — it always wins first, before this is
    # even consulted, so this helper must not also claim it.
    pane = "✻ Brewed for 4s · 1 shell still running"
    assert pane_shows_working(pane) is True


def test_shell_count_zero_when_absent():
    assert pane_shell_count("✻ Thinking… (esc to interrupt)") == 0
    assert pane_shell_count("") == 0


class TestSessionScopedTerminal:
    """Each session's terminal tab is a second window living inside that
    session's own tmux session — not one shell shared by every session."""

    def test_ensure_session_window_creates_it_once(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "0\n"  # only window 0 (claude) exists
            TmuxClient().ensure_session_window("apm_bundle", "term", "/work/apm")
        calls = [c[0][0] for c in run.call_args_list]
        new_window = next(c for c in calls if "new-window" in c)
        assert "=apm_bundle:" in new_window
        assert "term" in new_window and "/work/apm" in new_window

    def test_ensure_session_window_is_a_noop_if_already_present(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "0\nterm\n"
            TmuxClient().ensure_session_window("apm_bundle", "term", "/work/apm")
        calls = [c[0][0] for c in run.call_args_list]
        assert not any("new-window" in c for c in calls)

    def test_ensure_session_window_raises_if_the_session_is_gone(self):
        # Never silently no-op when the underlying session has vanished
        # (e.g. it died and cagents hasn't caught up yet) — a caller must
        # be able to surface this as a real error, not mistake it for
        # "the window already exists."
        import pytest

        with patch("subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "can't find session"
            with pytest.raises(RuntimeError):
                TmuxClient().ensure_session_window("apm_bundle", "term", "/work/apm")

    def test_ensure_window_view_creates_a_grouped_session_and_selects_it(self):
        with patch("subprocess.run") as run:
            run.side_effect = [
                _proc(1, ""),  # has-session on the group: not found yet
                _proc(0, ""),  # new-session -t (grouped)
                _proc(0, ""),  # select-window
            ]
            group = TmuxClient().ensure_window_view("apm_bundle", "term")
        assert group == "apm_bundle--term"
        calls = [c[0][0] for c in run.call_args_list]
        grouped_new = next(c for c in calls if "new-session" in c)
        assert "-t" in grouped_new and "=apm_bundle" in grouped_new
        select = next(c for c in calls if "select-window" in c)
        assert "=apm_bundle--term:term" in select

    def test_ensure_window_view_reuses_an_existing_group(self):
        with patch("subprocess.run") as run:
            run.side_effect = [
                _proc(0, ""),  # has-session: group already exists
                _proc(0, ""),  # select-window
            ]
            TmuxClient().ensure_window_view("apm_bundle", "term")
        calls = [c[0][0] for c in run.call_args_list]
        assert not any("new-session" in c for c in calls)


def _proc(returncode: int, stdout: str):
    from unittest.mock import Mock

    proc = Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    return proc
