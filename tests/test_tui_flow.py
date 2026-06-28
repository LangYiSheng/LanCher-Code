from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from lancher_code.errors import ProviderRequestError
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolCallChunk, ToolDefinition, ToolExecutionResult, TraceEntry
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui import (
    BannerWidget,
    ComposerTextArea,
    LanCherTextualApp,
    MessageWidget,
    ThinkingTraceWidget,
    _format_trace_entries,
)
from lancher_code.turn_runner import TurnRunner

DelayedEvent = tuple[StreamEvent, float]


class EchoTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="echo_tool", description="echo", input_schema={"type": "object"})

    async def execute(self, arguments: dict[str, object], context) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id="",
            tool_name=self.definition.name,
            ok=True,
            payload={"content": f"工具结果: {arguments['value']}"},
            summary=f"执行成功: {arguments['value']}",
        )


class FakeProvider:
    def __init__(self, responses: list[list[StreamEvent | DelayedEvent] | Exception]) -> None:
        self._responses = responses
        self.requests: list[ChatRequest] = []

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        current = self._responses.pop(0)
        if isinstance(current, Exception):
            raise current
        for item in current:
            if isinstance(item, tuple):
                event, delay = item
            else:
                event, delay = item, 0.0
            yield event
            if delay > 0:
                await asyncio.sleep(delay)


def _build_app(provider: FakeProvider, provider_config, ui_config, tmp_path: Path) -> tuple[LanCherTextualApp, SessionController]:
    session = SessionController(provider_config)
    registry = ToolRegistry()
    registry.register(EchoTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor)
    app = LanCherTextualApp(
        turn_runner=runner,
        provider_config=provider_config,
        session_controller=session,
        ui_config=ui_config,
    )
    return app, session


async def _submit_message(app: LanCherTextualApp, pilot, value: str) -> None:
    input_widget = app.query_one("#composer-input", ComposerTextArea)
    input_widget.text = value
    input_widget.focus()
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_composer_shift_enter_inserts_newline(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, _session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "第一行"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.press("shift+enter")
        composer.insert("第二行")
        await pilot.pause(0.05)

        assert composer.text == "第一行\n第二行"


@pytest.mark.asyncio
async def test_composer_grows_with_multiline_input_and_shrinks_after_submit(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="收到"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer_box = app.query_one("#composer", Horizontal)

        assert str(composer.styles.height) == "1"
        assert str(composer_box.styles.height) == "3"

        composer.text = "第一行\n第二行\n第三行"
        composer.focus()
        await pilot.pause(0.05)

        assert str(composer.styles.height) == "3"
        assert str(composer_box.styles.height) == "5"

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert str(composer.styles.height) == "1"
        assert str(composer_box.styles.height) == "3"


@pytest.mark.asyncio
async def test_tui_streams_single_turn_by_message_id(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="你好"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=3, output_tokens=2)),
            ]
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "你好")
        await pilot.pause(0.1)

        messages = list(app.query(MessageWidget))
        assert len(messages) == 2
        assert [message.content for message in session.state.messages] == ["你好", "你好"]
        assert [message.status for message in session.state.messages] == ["complete", "complete"]
        assert messages[-1].message_id == session.state.messages[-1].id
        assert len(provider.requests) == 1
        assert app.query_one("#composer-input", ComposerTextArea).text == ""
        status_right = app.query_one("#status-right", Static)
        assert "Tokens In 3 · Out 2" in str(status_right.render())


@pytest.mark.asyncio
async def test_tui_keeps_multi_turn_context_without_replacing_system_prompt(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="记住了"), StreamEvent(kind="message_end")],
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="我记得"), StreamEvent(kind="message_end")],
        ]
    )
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "记住我的名字是小兰")
        await pilot.pause(0.1)
        await _submit_message(app, pilot, "我叫什么名字？")
        await pilot.pause(0.1)

        assert len(provider.requests) == 2
        assert provider.requests[0].messages[0].blocks[0].text == provider.requests[1].messages[0].blocks[0].text


@pytest.mark.asyncio
async def test_tui_marks_error_message_and_excludes_it_from_later_context(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            ProviderRequestError("网络失败"),
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="恢复成功"), StreamEvent(kind="message_end")],
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "第一轮")
        await pilot.pause(0.1)
        await _submit_message(app, pilot, "第二轮")
        await pilot.pause(0.1)

        messages = list(app.query(MessageWidget))
        assert len(messages) == 4
        assert session.state.messages[1].status == "error"
        assert session.state.messages[1].content == "网络失败"
        assert session.state.messages[-1].content == "恢复成功"


@pytest.mark.asyncio
async def test_banner_collapses_after_first_submission(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="收到"), StreamEvent(kind="message_end")]
        ]
    )
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        banner = app.query_one(BannerWidget)
        assert banner.compact is False

        await _submit_message(app, pilot, "开始吧")
        await pilot.pause(0.1)

        banner = app.query_one(BannerWidget)
        assert banner.compact is True
        renderable = banner.render()
        assert isinstance(renderable, Text)
        assert "LanCher Code" in renderable.plain
        assert "工作目录：" in renderable.plain
        assert app.query_one("#chat-view", VerticalScroll).has_class("-banner-collapsed")


@pytest.mark.asyncio
async def test_thinking_trace_is_collapsed_by_default_and_can_expand(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                (StreamEvent(kind="thinking_delta", text="先整理一下思路"), 0.2),
                StreamEvent(kind="text_delta", text="这是正式回答"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "帮我想想")
        await pilot.pause(0.25)

        assistant = session.state.messages[-1]
        assert assistant.content == "这是正式回答"
        assert [entry.kind for entry in assistant.trace.entries] == ["thinking"]

        trace_widget = list(app.query(ThinkingTraceWidget))[-1]
        assert trace_widget.collapsed is True
        trace_widget.toggle_collapsed()
        await pilot.pause(0.05)
        assert trace_widget.collapsed is False
        rendered = trace_widget.query_one(".thinking-trace-body", Static).render()
        assert "先整理一下思路" in rendered.plain
        assert "思考：" not in rendered.plain

        formatted = _format_trace_entries(assistant.trace.entries)
        assert isinstance(formatted, Text)
        segments = {
            (formatted.plain[span.start : span.end], span.style)
            for span in formatted.spans
        }
        assert ("先整理一下思路", "#a8b9cc") in segments


@pytest.mark.asyncio
async def test_tui_renders_tool_flow_inside_thinking_trace(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="thinking_delta", text="先调用工具"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"hello"}')),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="thinking_delta", text="再整理一下"),
                StreamEvent(kind="text_delta", text="最终回答"),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "调用工具")
        await pilot.pause(0.2)

        assistant = session.state.messages[-1]
        assert assistant.content == "最终回答"
        assert [entry.kind for entry in assistant.trace.entries] == [
            "thinking",
            "tool_call",
            "tool_result",
            "thinking",
        ]

        trace_widget = list(app.query(ThinkingTraceWidget))[-1]
        trace_widget.toggle_collapsed()
        await pilot.pause(0.05)
        rendered = trace_widget.query_one(".thinking-trace-body", Static).render()
        assert "先调用工具" in rendered.plain
        assert "● echo_tool(value=hello)" in rendered.plain
        assert "✓ 执行成功: hello" in rendered.plain

        formatted = _format_trace_entries(assistant.trace.entries)
        assert isinstance(formatted, Text)
        segments = {
            (formatted.plain[span.start : span.end], span.style)
            for span in formatted.spans
        }
        assert ("先调用工具", "#a8b9cc") in segments
        assert ("● echo_tool(value=hello)", "#73b6ff") in segments
        assert ("✓ 执行成功: hello", "#78d98a") in segments
        assert ("再整理一下", "#a8b9cc") in segments


def test_format_trace_entries_renders_edit_preview_lines() -> None:
    formatted = _format_trace_entries(
        [
            TraceEntry(
                kind="tool_result",
                text="已修改文件 demo.txt",
                ok=True,
                metadata={
                    "display_lines": [
                        {"text": "- 12\told_value = 1", "tone": "error"},
                        {"text": "+ 12\tnew_value = 2", "tone": "success"},
                    ]
                },
            )
        ]
    )

    assert "已修改文件 demo.txt" in formatted.plain
    assert "- 12\told_value = 1" in formatted.plain
    assert "+ 12\tnew_value = 2" in formatted.plain
