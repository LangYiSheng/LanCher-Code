from __future__ import annotations

import pytest
from rich.console import Console
from rich.markdown import CodeBlock, Markdown
from rich.style import Style

from lancher_code.tui.theme import TerminalMarkdown, theme_palette


def _render(markdown: TerminalMarkdown, console: Console | None = None) -> tuple[str, list[Style]]:
    console = console or Console(width=80, color_system="truecolor", force_terminal=True)
    text = ""
    styles: list[Style] = []
    for segment in console.render(markdown):
        text += segment.text
        styles.extend([segment.style or Style.null()] * len(segment.text))
    return text, styles


def _styles_of(text: str, styles: list[Style], fragment: str) -> list[Style]:
    start = text.index(fragment)
    return styles[start:start + len(fragment)]


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("language", ["", "text", "python", "javascript", "unknown-lancher-language"])
def test_code_default_foreground_and_background_follow_application_theme(theme, language):
    text, styles = _render(TerminalMarkdown(f"```{language}\nvalue = 7\n```", theme))
    colors = theme_palette(theme)

    for style in _styles_of(text, styles, "value"):
        assert style.color is not None and style.color.name == colors["text"]
        assert style.bgcolor is not None and style.bgcolor.name == colors["surface"]
    for style in _styles_of(text, styles, "value = 7"):
        assert style.bgcolor is not None and style.bgcolor.name == colors["surface"]


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("language, snippet, token, color", [
    ("python", 'return "ready"  # note', "return", "primary"),
    ("python", 'return "ready"  # note', "ready", "success"),
    ("python", 'return "ready"  # note', "# note", "muted"),
    ("javascript", "const value = 7;", "const", "primary"),
    ("javascript", "const value = 7;", "7", "warning"),
    ("json", '{"status": "ready"}', "ready", "success"),
])
def test_syntax_highlighting_uses_readable_theme_colors(theme, language, snippet, token, color):
    text, styles = _render(TerminalMarkdown(f"```{language}\n{snippet}\n```", theme))
    colors = theme_palette(theme)
    for style in _styles_of(text, styles, token):
        assert style.color is not None and style.color.name == colors[color]
        assert style.bgcolor is not None and style.bgcolor.name == colors["surface"]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_markdown_headings_lists_links_inline_and_indented_code_are_preserved(theme):
    source = "## Heading\n\n    indented_code\n\n- ordinary body with `inline_code`\n- [link](https://example.test)\n"
    text, styles = _render(TerminalMarkdown(source, theme))
    colors = theme_palette(theme)

    assert "• ordinary body" in text
    assert all(style.bold for style in _styles_of(text, styles, "Heading"))
    for fragment in ("ordinary body", "inline_code", "indented_code"):
        assert all(style.color.name == colors["text"] for style in _styles_of(text, styles, fragment))
    for fragment in ("inline_code", "indented_code"):
        assert all(style.bgcolor.name == colors["surface"] for style in _styles_of(text, styles, fragment))
    assert all(style.link == "https://example.test" for style in _styles_of(text, styles, "link"))


def test_theme_switches_and_multiple_instances_do_not_leak_styles():
    console = Console(width=80, color_system="truecolor", force_terminal=True)
    original_inline_style = console.get_style("markdown.code")
    instances = [TerminalMarkdown("`inline`\n\n```\nvalue\n```", theme) for theme in ("light", "dark")]
    for instance in (instances[0], instances[1], instances[0]):
        text, styles = _render(instance, console)
        colors = theme_palette(instance.code_theme)
        for fragment in ("inline", "value"):
            for style in _styles_of(text, styles, fragment):
                assert style.color.name == colors["text"]
                assert style.bgcolor.name == colors["surface"]
        assert console.get_style("markdown.code") == original_inline_style
    assert Markdown.elements["fence"] is CodeBlock
    assert Markdown.elements["code_block"] is CodeBlock
