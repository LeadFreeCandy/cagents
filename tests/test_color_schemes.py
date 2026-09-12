"""Bundled Vim palettes and the public preview/save/cancel settings flow."""
import json

import pytest
from textual.widgets import OptionList

from cagents.app import CagentsApp
from cagents.store import Store
from conftest import FakeTmux


VIM_SCHEMES = set("blue catppuccin darkblue default delek desert elflord evening "
                  "habamax industry koehler lunaperche morning murphy novum pablo "
                  "peachpuff quiet retrobox ron shine slate sorbet torte unokai "
                  "wildcharm zaibatsu zellner".split())


def app_for(tmp_path):
    return CagentsApp(store=Store.load(tmp_path / "state.json"), tmux=FakeTmux(),
                     claude_dir=tmp_path / "claude", codex_dir=tmp_path / "codex")


def test_old_store_keeps_cagents_appearance_and_registers_every_vim_scheme(tmp_path):
    app = app_for(tmp_path)
    assert app.store.get_setting("color_scheme") == "cagents"
    assert app.theme == "textual-dark"
    assert {name.removeprefix("vim-") for name in app.available_themes if name.startswith("vim-")} == VIM_SCHEMES


@pytest.mark.parametrize("name", sorted(VIM_SCHEMES))
def test_saved_vim_scheme_loads_without_vim_or_network(tmp_path, name, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({"settings": {"color_scheme": name}}))
    monkeypatch.setenv("PATH", "")
    app = app_for(tmp_path)
    assert app.theme == "vim-" + name
    theme = app.current_theme
    assert theme.background and theme.foreground
    assert theme.background != theme.foreground
    assert theme.variables["block-cursor-background"] != theme.variables["block-cursor-foreground"]


@pytest.mark.parametrize("name,background,foreground,dark", [
    ("desert", "#333333", "#ffffff", True),
    ("peachpuff", "#ffdab9", "#000000", False),
    ("blue", "#000087", "#ffd700", True),
])
def test_vim_normal_colors_are_preserved(tmp_path, name, background, foreground, dark):
    app = app_for(tmp_path)
    app._setting_changed("color_scheme", name)
    assert app.current_theme.background == background
    assert app.current_theme.foreground == foreground
    assert app.current_theme.dark is dark


@pytest.mark.parametrize("saved", ["removed-scheme", None, 17, ["desert"], {"name": "desert"}])
def test_unknown_saved_scheme_falls_back_without_losing_other_settings(tmp_path, saved):
    (tmp_path / "state.json").write_text(json.dumps({"settings": {
        "color_scheme": saved, "conversation_title_width": 44}}))
    app = app_for(tmp_path)
    assert app.theme == "textual-dark"
    assert app.store.get_setting("conversation_title_width") == 44


async def test_settings_preview_cancel_save_and_restore_default(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.press("comma")
        settings = app.screen
        listing = settings.query_one("#settings-list", OptionList)
        assert "color_scheme" in {option.id for option in listing.options}
        listing.highlighted = listing.get_option_index("color_scheme")
        await pilot.press("enter")
        picker = app.screen.query_one("#color-schemes", OptionList)
        assert {option.id for option in picker.options} == VIM_SCHEMES | {"cagents"}
        picker.highlighted = picker.get_option_index("desert")
        await pilot.pause()
        assert app.theme == "vim-desert"
        assert app.screen.styles.background.hex == "#333333"
        assert Store.load(app.store.path).get_setting("color_scheme") == "cagents"
        await pilot.press("escape")
        assert app.screen is settings
        assert app.theme == "textual-dark"
        assert app.store.get_setting("color_scheme") == "cagents"

        await pilot.press("enter")
        picker = app.screen.query_one("#color-schemes", OptionList)
        picker.highlighted = picker.get_option_index("peachpuff")
        await pilot.press("enter")
        assert app.screen is settings and app.theme == "vim-peachpuff"
        assert Store.load(app.store.path).get_setting("color_scheme") == "peachpuff"
        assert listing.get_option_at_index(listing.highlighted).id == "color_scheme"
        await pilot.press("escape")
        assert app.theme == "vim-peachpuff"

    reopened = app_for(tmp_path)
    assert reopened.theme == "vim-peachpuff"
    async with reopened.run_test(size=(100, 35)) as pilot:
        await pilot.press("comma")
        listing = reopened.screen.query_one("#settings-list", OptionList)
        listing.highlighted = listing.get_option_index("color_scheme")
        await pilot.press("enter")
        picker = reopened.screen.query_one("#color-schemes", OptionList)
        assert picker.get_option_at_index(picker.highlighted).id == "peachpuff"
        picker.highlighted = picker.get_option_index("cagents")
        await pilot.press("enter")
        assert reopened.theme == "textual-dark"
        assert Store.load(reopened.store.path).get_setting("color_scheme") == "cagents"


async def test_cancelling_scheme_preview_restores_the_actual_previous_theme(tmp_path):
    app = app_for(tmp_path)
    app.theme = "nord"  # a choice made through Textual's existing command palette
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.press("comma")
        listing = app.screen.query_one("#settings-list", OptionList)
        assert "color_scheme" in {option.id for option in listing.options}
        listing.highlighted = listing.get_option_index("color_scheme")
        await pilot.press("enter")
        picker = app.screen.query_one("#color-schemes", OptionList)
        picker.highlighted = picker.get_option_index("blue")
        await pilot.pause()
        assert app.theme == "vim-blue"
        await pilot.press("escape")
        assert app.theme == "nord"


async def test_every_scheme_renders_settings_and_readable_selection(tmp_path):
    app = app_for(tmp_path)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.press("comma")
        listing = app.screen.query_one("#settings-list", OptionList)
        for name in sorted(VIM_SCHEMES):
            app._setting_changed("color_scheme", name)
            await pilot.pause()
            assert app.theme == "vim-" + name
            selected = listing.get_component_styles("option-list--option-highlighted")
            assert selected.color != selected.background, name
            assert listing.render_line(0).text.strip(), name
