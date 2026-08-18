"""Tests for the priority-fill footer (footer.py) — pure logic, no TUI."""

from __future__ import annotations

from cagents.footer import FOOTER_PRIORITY, FooterItem, render_line, visible_items


def test_narrow_width_keeps_only_the_highest_priority_core_loop_items():
    items = visible_items(width=20)
    keys = [i.key for i in items]
    assert keys[0] == "enter"
    assert "F" in keys  # Fork — fits right after Attach even this narrow
    # something must have been dropped to fit — the list isn't exhaustive
    assert len(items) < len(FOOTER_PRIORITY)


def test_fork_and_done_always_present_whenever_anything_fits_past_attach():
    # Even a middling width — not just a huge one — must keep the core loop.
    for width in (30, 40, 60, 100, 200):
        items = visible_items(width=width)
        keys = [i.key for i in items]
        assert "F" in keys, f"Fork missing at width={width}"
        assert "r" in keys, f"Done missing at width={width}"


def test_wide_enough_fits_everything_not_gated():
    items = visible_items(width=1000)
    assert len(items) == len(FOOTER_PRIORITY)


def test_zero_or_negative_width_yields_nothing():
    assert visible_items(width=0) == []
    assert visible_items(width=-5) == []


def test_a_short_lower_priority_item_can_fit_after_a_skipped_long_one():
    # Craft a priority list where a long item is immediately followed by a
    # short one that should still make it in if the long one didn't fit.
    items = [
        FooterItem("enter", "Attach"),  # "enter Attach" = 12 wide
        FooterItem("H", "Handoff-and-then-some-long-label"),  # way too wide to fit
        FooterItem("q", "Q"),  # short — should still be picked up after H is skipped
    ]
    chosen = visible_items(width=17, items=items)
    keys = [i.key for i in chosen]
    assert "enter" in keys
    assert "H" not in keys
    assert "q" in keys


def test_gated_item_hidden_when_setting_off_present_when_on():
    gated = FooterItem("4", "Todos", setting="todos_enabled")
    items = [FooterItem("enter", "Attach"), gated]
    off = visible_items(width=100, items=items, setting_enabled=lambda k: False)
    on = visible_items(width=100, items=items, setting_enabled=lambda k: True)
    assert gated not in off
    assert gated in on


def test_todos_item_in_the_real_priority_list_is_gated_by_todos_enabled():
    todos = next(i for i in FOOTER_PRIORITY if i.label == "Todos")
    assert todos.setting == "todos_enabled"


def test_render_line_includes_every_chosen_labels_text():
    items = [FooterItem("enter", "Attach"), FooterItem("F", "Fork")]
    text = render_line(items)
    rendered = text.plain
    assert "enter" in rendered
    assert "Attach" in rendered
    assert "F" in rendered
    assert "Fork" in rendered


def test_render_line_of_empty_list_is_empty():
    assert render_line([]).plain == ""
