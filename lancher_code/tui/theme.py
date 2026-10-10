"""聊天、首次配置和设置共用的原生终端主题。"""

from __future__ import annotations

from rich.console import Console, ConsoleOptions, RenderResult
from pygments.token import Comment, Error, Generic, Keyword, Name, Number, String, Token
from rich.markdown import CodeBlock, Markdown
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
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


class _TerminalSyntaxTheme(SyntaxTheme):
    """代码的默认文字、底色和语法色均绑定当前应用主题。"""

    def __init__(self, theme_name: str) -> None:
        colors = theme_palette(theme_name)
        self._base = Style(color=colors["text"], bgcolor=colors["surface"])
        self._tokens = {
            Token: self._base,
            Comment: self._base + Style(color=colors["muted"]),
            Keyword: self._base + Style(color=colors["primary"], bold=True),
            Name.Function: self._base + Style(color=colors["primary"]),
            Name.Class: self._base + Style(color=colors["primary"]),
            String: self._base + Style(color=colors["success"]),
            Number: self._base + Style(color=colors["warning"]),
            Generic.Inserted: self._base + Style(color=colors["success"]),
            Generic.Deleted: self._base + Style(color=colors["error"]),
            Generic.Heading: self._base + Style(bold=True),
            Generic.Subheading: self._base + Style(bold=True),
            Error: self._base + Style(color=colors["error"], underline=True),
        }

    def get_style_for_token(self, token_type: tuple[str, ...]) -> Style:
        # 未单独着色的 token 继承应用正文色，避免 Pygments 回退成纯黑。
        while token_type not in self._tokens:
            token_type = token_type[:-1]
        return self._tokens[token_type]

    def get_background_style(self) -> Style:
        # 未知语言没有 lexer，Syntax 会直接使用此基础样式绘制整段。
        return self._base


class _TerminalCodeBlock(CodeBlock):
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield Syntax(
            str(self.text).rstrip(), self.lexer_name,
            theme=_TerminalSyntaxTheme(self.theme), word_wrap=True, padding=1,
        )


class TerminalMarkdown(Markdown):
    """正文、标题与代码沿用应用主题，不引入另一套终端强调色。"""

    # 单独复制映射；保留普通 Rich Markdown 以及其他实例的默认行为。
    elements = {**Markdown.elements, "fence": _TerminalCodeBlock, "code_block": _TerminalCodeBlock}

    def __init__(self, text: str, theme_name: str = "dark") -> None:
        colors = theme_palette(theme_name)
        super().__init__(text, code_theme=theme_name, style=colors["text"])
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
