"""聊天、首次配置和设置共用的原生终端主题。"""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme


PALETTES = {
    "dark": {
        "text": "#e4e7ec", "muted": "#929aa7", "primary": "#83b6f6",
        "background": "#15181d", "surface": "#20252d", "panel": "#252c36",
        "warning": "#e8bd72", "error": "#ed8d91", "success": "#93c7a0",
    },
    "light": {
        "text": "#242b35", "muted": "#647080", "primary": "#285fa5",
        "background": "#fafaf8", "surface": "#eef0f3", "panel": "#e3e8ef",
        "warning": "#8a5a09", "error": "#aa3541", "success": "#367345",
    },
}


def theme_palette(theme_name: str = "dark") -> dict[str, str]:
    return PALETTES["light" if theme_name in {"light", "lancher-light"} else "dark"]


def apply_theme(app: App, theme_name: str = "dark") -> None:
    """将同一套语义色用于 Textual 控件和 Rich 文本。"""
    name = "light" if theme_name in {"light", "lancher-light"} else "dark"
    colors = theme_palette(name)
    app.register_theme(Theme(
        name=f"lancher-{name}", dark=name == "dark", primary=colors["primary"],
        secondary=colors["primary"], accent=colors["primary"],
        foreground=colors["text"], background=colors["background"],
        surface=colors["surface"], panel=colors["panel"],
        warning=colors["warning"], error=colors["error"], success=colors["success"],
        variables={"text-muted": colors["muted"], "footer-background": colors["background"]},
    ))
    app.theme = f"lancher-{name}"
