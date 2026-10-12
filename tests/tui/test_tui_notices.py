from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Static

from lancher_code.config.models import UIConfig
from lancher_code.sessions.models import TraceEntry
from lancher_code.tui.theme import TerminalMarkdown, theme_palette
from lancher_code.tui.timeline import NoticeWidget, ThinkingTraceWidget
from test_tui_flow import FakeProvider, _build_app


def _plain(widget: Static) -> str:
    output = StringIO()
    Console(file=output, width=100, color_system=None).print(widget.content)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("theme,show_thinking,size", [
    ("dark", True, (100, 40)), ("light", False, (32, 24)),
])
async def test_persistent_notices_keep_severity_markdown_and_body_after_failure(
    openai_provider_config, tmp_path, theme, show_thinking, size,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config,
                              UIConfig(theme=theme, show_thinking_status=show_thinking), tmp_path)
    message = session.create_assistant_message()
    session.append_trace_thinking(message.id, "思考第一行\n思考第二行")
    session.append_message_content(message.id, "已完成的 **部分输出**。")
    session.append_trace_notice(message.id, "达到输出上限，**正在续接**。", code="output_limit")
    session.fail_message(message.id, "连接失败，请输入 `继续` 重试。")
    async with app.run_test(size=size) as pilot:
        await app._transcript.mount_message(message)
        await pilot.pause()
        widget = app._transcript.message_widgets[message.id]
        notices = list(widget.query(NoticeWidget))
        assert [notice.severity for notice in notices] == ["warning", "error"]
        assert all(notice.display and notice.region.height > 1 for notice in notices)
        assert widget.query_one(ThinkingTraceWidget).display is show_thinking
        for notice, severity in zip(notices, ("warning", "error"), strict=True):
            header, body = tuple(notice.content.renderables)
            assert isinstance(header, Text)
            assert header.style == "bold " + theme_palette(theme)[severity]
            assert isinstance(body, TerminalMarkdown)
        assert "正在续接" in _plain(notices[0]) and "**" not in _plain(notices[0])
        assert "继续" in _plain(notices[1]) and "`" not in _plain(notices[1])
        texts = [child for child in widget.query_one(".message-timeline").children
                 if child.has_class("timeline-text") and not isinstance(child, NoticeWidget)]
        assert len(texts) == 1 and "部分输出" in _plain(texts[0])
        assert not widget.query_one(".message-body").display


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["error", "cancelled"])
async def test_thinking_only_terminal_message_has_one_visible_notice(
    openai_provider_config, tmp_path, terminal,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config,
                              UIConfig(show_thinking_status=False), tmp_path)
    message = session.create_assistant_message()
    session.append_trace_thinking(message.id, "仅有思考，尚无正文")
    if terminal == "error":
        session.fail_message(message.id, "请求失败，**原因明确**。")
    else:
        session.cancel_message(message.id)
    async with app.run_test() as pilot:
        await app._transcript.mount_message(message)
        await pilot.pause()
        widget = app._transcript.message_widgets[message.id]
        notice = widget.query_one(NoticeWidget)
        assert len(widget.query(NoticeWidget)) == 1
        assert notice.display and notice.region.height > 0
        assert notice.severity == ("error" if terminal == "error" else "info")
        assert not widget.query_one(".message-body").display
        await app._transcript.restore()
        assert len(app._transcript.message_widgets[message.id].query(NoticeWidget)) == 1


@pytest.mark.asyncio
async def test_completion_collapse_keeps_warning_visible(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    session.append_trace_thinking(message.id, "第一行\n第二行")
    session.append_trace_notice(message.id, "曾达到上限，回答已自动完成。", code="output_limit")
    session.append_message_content(message.id, "最终回答")
    session.complete_message(message.id, record_transcript=False)
    async with app.run_test() as pilot:
        await app._transcript.mount_message(message)
        await pilot.pause()
        widget = app._transcript.message_widgets[message.id]
        assert widget.query_one(ThinkingTraceWidget).collapsed
        assert widget.query_one(NoticeWidget).display
        widget.collapse_for_completion()
        assert widget.query_one(NoticeWidget).display


@pytest.mark.asyncio
async def test_error_projection_fallback_is_visible_without_mutating_trace(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config,
                              UIConfig(show_thinking_status=False), tmp_path)
    message = session.create_assistant_message()
    original = TraceEntry(kind="thinking", text="只保存了思考", metadata={"state": "error"})
    message.trace.entries = [original]
    message.status = "error"
    async with app.run_test() as pilot:
        await app._transcript.mount_message(message)
        await pilot.pause()
        widget = app._transcript.message_widgets[message.id]
        notice = widget.query_one(NoticeWidget)
        assert "请求失败" in _plain(notice)
        assert notice.display and notice.severity == "error"
        assert message.trace.entries == [original]


@pytest.mark.asyncio
async def test_system_error_uses_same_notice_and_markdown_renderer(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.role = "system"
    message.status = "error"
    message.content = "**保存失败**，请检查 `config.yaml`。"
    async with app.run_test() as pilot:
        await app._transcript.mount_message(message)
        await pilot.pause()
        widget = app._transcript.message_widgets[message.id]
        notice = widget.query_one(NoticeWidget)
        assert notice.severity == "error"
        assert "保存失败" in _plain(notice) and "**" not in _plain(notice)
        assert "config.yaml" in _plain(notice) and "`" not in _plain(notice)
        assert not widget.query_one(".message-body").display
