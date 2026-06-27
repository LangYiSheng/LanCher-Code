from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from lancher_code.errors import ProviderRequestError
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent
from lancher_code.session import SessionController
from lancher_code.tui import BannerWidget, ComposerTextArea, LanCherTextualApp, MessageWidget

DelayedEvent = tuple[StreamEvent, float]


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


async def _submit_message(app: LanCherTextualApp, pilot, value: str) -> None:
    input_widget = app.query_one("#composer-input", ComposerTextArea)
    input_widget.text = value
    input_widget.focus()
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_composer_shift_enter_inserts_newline(
    openai_provider_config,
    ui_config,
) -> None:
    provider = FakeProvider(responses=[])
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

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
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="text_delta", text="收到"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

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
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

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
async def test_tui_keeps_multi_turn_context_without_streaming_messages(
    openai_provider_config,
    ui_config,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="text_delta", text="记住了"),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="text_delta", text="我记得"),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "记住我的名字是小兰")
        await pilot.pause(0.1)
        await _submit_message(app, pilot, "我叫什么名字？")
        await pilot.pause(0.1)

        assert len(provider.requests) == 2
        assert [message.content for message in provider.requests[1].messages] == [
            "记住我的名字是小兰",
            "记住了",
            "我叫什么名字？",
        ]


@pytest.mark.asyncio
async def test_tui_marks_error_message_and_excludes_it_from_later_context(
    openai_provider_config,
    ui_config,
) -> None:
    provider = FakeProvider(
        responses=[
            ProviderRequestError("网络失败"),
            [
                StreamEvent(kind="text_delta", text="恢复成功"),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "第一轮")
        await pilot.pause(0.1)
        await _submit_message(app, pilot, "第二轮")
        await pilot.pause(0.1)

        messages = list(app.query(MessageWidget))
        assert len(messages) == 4
        assert [message.role for message in session.state.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert session.state.messages[1].status == "error"
        assert session.state.messages[1].content == "网络失败"
        assert session.state.messages[-1].content == "恢复成功"
        assert [message.content for message in provider.requests[1].messages] == [
            "第一轮",
            "第二轮",
        ]


@pytest.mark.asyncio
async def test_banner_collapses_after_first_submission(
    openai_provider_config,
    ui_config,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="text_delta", text="收到"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

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
async def test_thinking_is_shown_before_answer_and_then_collapsed(
    openai_provider_config,
    ui_config,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                (StreamEvent(kind="thinking_delta", text="先整理一下思路"), 0.2),
                StreamEvent(kind="text_delta", text="这是正式回答"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    session = SessionController(openai_provider_config)
    app = LanCherTextualApp(
        provider=provider,
        provider_config=openai_provider_config,
        session_controller=session,
        ui_config=ui_config,
    )

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "帮我想想")
        await pilot.pause(0.05)

        assistant = list(app.query(MessageWidget))[-1]
        assert assistant.role == "assistant"
        assert assistant.thinking == "先整理一下思路"
        assert assistant.thinking_collapsed is False
        assert assistant.content == ""

        await pilot.pause(0.25)

        assistant = list(app.query(MessageWidget))[-1]
        assert assistant.content == "这是正式回答"
        assert assistant.thinking == "先整理一下思路"
        assert assistant.thinking_collapsed is True
        assert assistant.query_one(".message-thinking", Static).display is False
