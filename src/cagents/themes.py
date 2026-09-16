"""Vim's bundled GUI palettes adapted to Textual and the cagents tab bar.

The palette data is shipped with cagents; selecting a scheme never runs Vim.
See data/VIM_NOTICE.txt for the upstream revision and license.
"""
from importlib.resources import files
import json

from textual.theme import Theme


VIM_PALETTES = json.loads(files("cagents").joinpath("data/vim_colors.json").read_text())["palettes"]
COLOR_SCHEMES = ("cagents", *sorted(VIM_PALETTES))


def theme_name(scheme: str) -> str:
    return "vim-" + scheme if scheme in VIM_PALETTES else "textual-dark"


def vim_themes():
    for name, palette in VIM_PALETTES.items():
        dark = bool(palette["dark"])
        groups = palette["groups"]
        normal = groups["Normal"]
        foreground = normal["fg"] or ("#ffffff" if dark else "#000000")
        background = normal["bg"] or ("#000000" if dark else "#ffffff")

        def colors(group):
            values = groups[group]
            fg, bg = values["fg"] or foreground, values["bg"] or background
            return (bg, fg) if values["reverse"] else (fg, bg)

        selected_fg, selected_bg = colors("PmenuSel")
        if selected_fg == selected_bg:
            selected_fg, selected_bg = background, foreground
        status_fg, status_bg = colors("StatusLine")
        visual_fg, visual_bg = colors("Visual")
        yield Theme(
            name="vim-" + name, dark=dark, foreground=foreground, background=background,
            surface=background, panel=colors("Pmenu")[1],
            primary=colors("Statement")[0], secondary=colors("Identifier")[0],
            accent=colors("Comment")[0], warning=colors("WarningMsg")[0],
            error=colors("ErrorMsg")[1] if groups["ErrorMsg"]["reverse"] else colors("ErrorMsg")[0],
            success=colors("MoreMsg")[0], text_alpha=1.0,
            variables={
                "block-cursor-background": selected_bg,
                "block-cursor-foreground": selected_fg,
                "block-cursor-blurred-background": selected_bg,
                "block-cursor-blurred-foreground": selected_fg,
                "block-cursor-text-style": "bold",
                "block-cursor-blurred-text-style": "none",
                "input-selection-background": visual_bg,
                "input-selection-foreground": visual_fg,
                "input-cursor-background": foreground,
                "input-cursor-foreground": background,
                "footer-background": status_bg,
                "footer-foreground": status_fg,
                "footer-key-foreground": status_fg,
                "footer-description-foreground": status_fg,
                "border": colors("Statement")[0],
                "border-blurred": colors("LineNr")[0],
            },
        )


def tab_color_commands(theme: Theme) -> list[list[str]]:
    if theme.name.startswith("vim-"):
        values = theme.variables
        status = f'bg={values["footer-background"]},fg={values["footer-foreground"]}'
        selected = f'bg={values["block-cursor-background"]},fg={values["block-cursor-foreground"]}'
    else:
        status, selected = "bg=colour236,fg=colour248", "bg=colour31,fg=colour231"
    return [["set", "-g", "status-style", status],
            ["set", "-g", "window-status-current-format", f"#[{selected},bold]  #W  #[default]"]]
