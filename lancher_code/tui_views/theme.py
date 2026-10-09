"""聊天、首次配置和设置共用的原生终端主题。"""

from __future__ import annotations

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.theme import Theme as RichTheme
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


class TerminalMarkdown(Markdown):
    """正文、标题与代码沿用应用主题，不引入另一套终端强调色。"""

    def __init__(self, text: str, theme_name: str = "dark") -> None:
        colors = theme_palette(theme_name)
        light = theme_name in {"light", "lancher-light"}
        super().__init__(text, code_theme="default" if light else "monokai", style=colors["text"])
        self._text_theme = RichTheme({
            **{f"markdown.h{level}": "bold " + colors["text"] for level in range(1, 8)},
            "markdown.h1.border": colors["panel"],
            "markdown.code": f'{colors["text"]} on {colors["surface"]}',
            "markdown.code_block": colors["text"],
            "markdown.block_quote": colors["muted"],
            "markdown.list": colors["text"],
            "markdown.item.number": colors["text"],
            "markdown.hr": colors["panel"],
            "markdown.link": colors["primary"],
            "markdown.link_url": "underline " + colors["primary"],
            "markdown.table.border": colors["panel"],
            "markdown.table.header": "bold " + colors["text"],
        })

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # 在本次渲染内解析颜色，不修改其他控件或终端的全局主题。
        with console.use_theme(self._text_theme):
            yield from super().__rich_console__(console, options)


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
