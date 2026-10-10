from __future__ import annotations

from lancher_code.tui_views.message import ToolActivityWidget, ToolCallWidget

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from lancher_code.errors import ProviderRequestError
from lancher_code.models import (
    ChatRequest,
    MessageUsage,
    StreamEvent,
    ToolCallChunk,
    ToolDefinition,
    ToolExecutionResult,
    TraceEntry,
    TurnEvent,
)
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui import (
    BannerWidget,
    CommandHintBar,
    ComposerTextArea,
    LanCherTextualApp,
    MessageWidget,
    SlashCompletionMenu,
    SlashCompletionMenuItem,
    ThinkingTraceWidget,
    _format_trace_entries,
    ComposerSubmitted,
)
from lancher_code.mcp.manager import MCPInitializationProgress
from lancher_code.turn_runner import TurnRunner
from lancher_code.tui_views.chat import CONTEXT_REFRESH_EVENTS

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
    session = SessionController(provider_config, cwd=tmp_path)
    registry = ToolRegistry()
    registry.register(EchoTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor)
    app = LanCherTextualApp(
        turn_runner=runner,
        provider_config=provider_config,
        session_controller=session,
        ui_config=ui_config,
        tool_registry=registry,
    )
    return app, session


async def _submit_message(app: LanCherTextualApp, pilot, value: str) -> None:
    input_widget = app.query_one("#composer-input", ComposerTextArea)
    input_widget.text = value
    input_widget.focus()
    await pilot.press("enter")


@pytest.mark.parametrize(
    ("used_tokens", "context_window", "expected"),
    [
        (0, 1_000, "上下文 0%"),
        (1, 1_000, "上下文 1%"),
        (370, 1_000, "上下文 37%"),
        (1_001, 1_000, "上下文 100%"),
        (None, 1_000, "上下文 --"),
        (100, 0, "上下文 --"),
    ],
)
def test_banner_formats_context_window_usage(
    tmp_path: Path,
    used_tokens: int | None,
    context_window: int,
    expected: str,
) -> None:
    banner = BannerWidget(tmp_path)

    banner.update_context_usage(used_tokens, context_window)

    assert banner._context_usage_status == expected


def test_context_usage_refresh_events_exclude_streaming_text_deltas() -> None:
    assert {
        "user_message_created",
        "usage_updated",
        "tool_result_received",
        "progress_updated",
        "assistant_message_completed",
    } <= CONTEXT_REFRESH_EVENTS
    assert "assistant_text_delta" not in CONTEXT_REFRESH_EVENTS


@pytest.mark.asyncio
async def test_context_usage_is_not_reestimated_for_streaming_text_delta(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session = _build_app(
        FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path
    )
    estimate_calls = 0

    def estimate_request_tokens(_request: ChatRequest) -> int:
        nonlocal estimate_calls
        estimate_calls += 1
        return 37_000

    monkeypatch.setattr(session, "estimate_request_tokens", estimate_request_tokens)

    async with app.run_test():
        initial_calls = estimate_calls
        assert app.query_one(BannerWidget)._context_usage_status == "上下文 29%"

        await app._consume_turn_event(TurnEvent(kind="assistant_text_delta"))
        assert estimate_calls == initial_calls

        await app._consume_turn_event(TurnEvent(kind="usage_updated"))
        assert estimate_calls == initial_calls + 1


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
async def test_tui_composer_placeholder_is_minimal_in_normal_mode(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, _session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test():
        composer = app.query_one("#composer-input", ComposerTextArea)
        status_left = app.query_one("#status-left", Static)
        assert composer.placeholder == "发送一条消息"
        assert str(status_left.render()) == "gpt-test · 执行 · 逐次确认"
        hint_bar = app.query_one(CommandHintBar)
        assert not hint_bar.display


@pytest.mark.asyncio
async def test_chat_view_keeps_balanced_horizontal_gutters(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, _ = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test(size=(100, 30)):
        chat_view = app.query_one("#chat-view", VerticalScroll)
        assert chat_view.region.x == 1
        assert chat_view.region.right == 99
        assert chat_view.content_region.x >= 2


@pytest.mark.asyncio
async def test_shift_tab_cycles_work_phase_without_changing_permission(
    openai_provider_config, ui_config, tmp_path: Path,
) -> None:
    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)
    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.focus()
        for expected in ("discuss", "plan", "execute"):
            await pilot.press("shift+tab")
            await pilot.pause(0.05)
            assert session.work_phase == expected
            assert session.permission_policy == "default"
            assert "gpt-test" in str(app.query_one("#status-left", Static).render())
            assert app.query_one(f"#phase-{expected}").has_class("-selected")


def _visible_slash_commands(app: LanCherTextualApp) -> list[str]:
    return [
        item.candidate.value
        for item in app.query(SlashCompletionMenuItem)
        if item.display
    ]


@pytest.mark.asyncio
async def test_session_resume_rebuilds_chat_widgets(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    saved = SessionController(openai_provider_config, cwd=tmp_path)
    saved.create_user_message("历史问题")
    reply = saved.create_assistant_message()
    saved.append_message_content(reply.id, "历史回答")
    saved.complete_message(reply.id)
    saved_id = saved.session_id
    saved.close()

    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)
    async with app.run_test() as pilot:
        await app._execute_session_command(f"resume {saved_id}")
        await pilot.pause()

        assert session.session_id == saved_id
        assert [widget.message_id for widget in app.query(MessageWidget)] == [
            message.id for message in session.state.messages
        ]
        assert app.query_one(BannerWidget).compact is True


@pytest.mark.asyncio
async def test_mcp_initialization_gates_input_and_restores_composer(
    openai_provider_config, ui_config, tmp_path: Path,
) -> None:
    class FakeManager:
        has_servers = True

        def __init__(self) -> None:
            self.callbacks = []
            self.release = asyncio.Event()

        def add_progress_callback(self, callback) -> None:
            self.callbacks.append(callback)

        async def initialize(self, registry) -> None:
            for callback in self.callbacks:
                callback(MCPInitializationProgress(1, 0, 0, 0, 0, "demo", "connecting"))
            await self.release.wait()
            for callback in self.callbacks:
                callback(MCPInitializationProgress(1, 1, 1, 0, 2, None, "complete"))

    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)
    manager = FakeManager()
    app._mcp_manager = manager  # type: ignore[assignment]
    app._tool_registry = object()  # type: ignore[assignment]
    app.mcp_initialization_complete = False

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        await pilot.pause(0.05)
        assert composer.disabled
        assert composer.placeholder == "正在初始化 MCP，请稍候…"
        await app.handle_input_submitted(ComposerSubmitted(composer, "不应发送"))
        assert session._state.messages == []
        manager.release.set()
        await pilot.pause(0.05)
        assert app.mcp_initialization_complete
        assert not composer.disabled
        assert composer.placeholder == "发送一条消息"
        assert "2 个工具" in app.query_one(BannerWidget)._mcp_status


@pytest.mark.asyncio
async def test_slash_menu_opens_and_filters_in_normal_mode(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, _session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.pause(0.05)

        menu = app.query_one(SlashCompletionMenu)
        assert menu.display
        assert _visible_slash_commands(app) == [
            "discuss", "plan", "do", "session", "tasks", "model", "permissions",
            "compact", "settings", "status", "exit",
        ]

        composer.text = "/p"
        composer.cursor_location = composer.document.end
        await pilot.pause(0.05)
        assert _visible_slash_commands(app) == ["plan", "permissions"]

        composer.text = "/d"
        composer.cursor_location = composer.document.end
        await pilot.pause(0.05)
        assert _visible_slash_commands(app) == ["discuss", "do"]


@pytest.mark.asyncio
async def test_compact_command_uses_summary_request_without_creating_user_message(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headings = (
        "主要请求和意图",
        "关键技术概念",
        "文件和代码段",
        "错误与修复",
        "问题解决过程",
        "用户消息与明确反馈",
        "待办任务",
        "当前工作",
        "可能的下一步",
    )
    summary = "<summary>" + "\n".join(f"## {heading}\n内容" for heading in headings) + "</summary>"
    provider = FakeProvider(
        responses=[
            [StreamEvent(kind="text_delta", text=summary), StreamEvent(kind="message_end")]
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)
    session.create_user_message("已有历史")
    message_count = len(session.state.messages)
    context_refresh_count = 0
    original_refresh = app._refresh_context_usage

    def track_context_refresh() -> None:
        nonlocal context_refresh_count
        context_refresh_count += 1
        original_refresh()

    monkeypatch.setattr(app, "_refresh_context_usage", track_context_refresh)

    async with app.run_test() as pilot:
        refresh_count_before_compact = context_refresh_count
        await _submit_message(app, pilot, "/compact")
        await pilot.pause(0.1)

        assert len(provider.requests) == 1
        assert provider.requests[0].allow_tool_calls is False
        assert len(session.state.messages) == message_count
        assert app.query_one("#composer-input", ComposerTextArea).disabled is False
        assert context_refresh_count == refresh_count_before_compact + 1


@pytest.mark.asyncio
async def test_slash_menu_accepts_selection_without_submitting(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=[])
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.pause(0.05)
        await pilot.press("enter")
        await pilot.pause(0.05)

        assert composer.text == "/discuss "
        assert len(provider.requests) == 0
        assert not app.query_one(SlashCompletionMenu).display


@pytest.mark.asyncio
async def test_slash_menu_accepts_selection_with_tab_without_submitting(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(responses=[])
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.pause(0.05)
        await pilot.press("tab")
        await pilot.pause(0.05)

        assert composer.text == "/discuss "
        assert len(provider.requests) == 0
        assert not app.query_one(SlashCompletionMenu).display


@pytest.mark.asyncio
async def test_multilevel_session_completion_advances_until_terminal_value(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    saved = SessionController(openai_provider_config, cwd=tmp_path)
    saved.create_user_message("历史任务")
    saved_id = saved.session_id
    saved.close()
    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/ses"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.pause(0.05)
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert composer.text == "/session "
        assert _visible_slash_commands(app) == ["new", "list", "stop", "resume", "rename", "archive", "remove"]

        composer.text = "/session res"
        composer.cursor_location = composer.document.end
        await pilot.pause(0.05)
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert composer.text == "/session resume "
        assert _visible_slash_commands(app) == [saved_id]

        await pilot.press("tab")
        await pilot.pause(0.05)
        assert composer.text == f"/session resume {saved_id}"
        assert not app.query_one(SlashCompletionMenu).display

        await pilot.press("enter")
        await pilot.pause(0.05)
        assert session.session_id == saved_id


@pytest.mark.asyncio
async def test_permission_completion_selects_terminal_value_before_submission(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)
    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/permissions b"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.pause(0.05)
        await pilot.press("tab")
        await pilot.pause(0.05)

        assert composer.text == "/permissions bypass"
        assert not app.query_one(SlashCompletionMenu).display
        assert session.runtime_mode == "default"

        await pilot.press("enter")
        await pilot.pause(0.05)
        assert session.permission_policy == "bypass"
        assert session.work_phase == "execute"


@pytest.mark.asyncio
async def test_escape_closes_slash_menu(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, _session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/"
        composer.cursor_location = composer.document.end
        composer.focus()
        await pilot.pause(0.05)
        await pilot.press("escape")
        await pilot.pause(0.05)

        assert not app.query_one(SlashCompletionMenu).display


@pytest.mark.asyncio
async def test_command_hint_updates_for_selected_command(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    app, _session = _build_app(FakeProvider(responses=[]), openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", ComposerTextArea)
        composer.text = "/plan "
        composer.focus()
        await pilot.pause(0.05)

        hint_bar = app.query_one(CommandHintBar)
        assert hint_bar.display
        assert "制定计划" in str(hint_bar.render())
        assert "参数可选：任务描述" in str(hint_bar.render())


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
        assert str(composer_box.styles.height) == "2"

        composer.text = "第一行\n第二行\n第三行"
        composer.focus()
        await pilot.pause(0.05)

        assert str(composer.styles.height) == "3"
        assert str(composer_box.styles.height) == "4"

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert str(composer.styles.height) == "1"
        assert str(composer_box.styles.height) == "2"


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
        status_left = app.query_one("#status-left", Static)
        status_right = app.query_one("#status-details", Static)
        assert str(status_left.render()) == "gpt-test · 执行 · 逐次确认"
        assert "本次模型：gpt-test" in str(status_right.render())
        assert "Tokens In 3" in str(status_right.render())
        assert "cached" not in str(status_right.render())
        assert "Out 2" in str(status_right.render())


@pytest.mark.asyncio
async def test_tui_shows_cached_input_tokens_when_present(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="浣犲ソ"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=3, cached_input_tokens=1, output_tokens=2)),
            ]
        ]
    )
    app, _session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "浣犲ソ")
        await pilot.pause(0.1)

        status_right = app.query_one("#status-details", Static)
        assert "Tokens In 3 (cached 1)" in str(status_right.render())
        assert "Out 2" in str(status_right.render())


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
        assert provider.requests[0].system == provider.requests[1].system


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
        console = Console(width=120, color_system=None)
        with console.capture() as capture:
            console.print(renderable)
        header_text = capture.get()
        assert "LanCher Code" in header_text
        assert banner._cwd.name in header_text
        assert "MCP" not in header_text
        assert "上下文" not in header_text
        assert len(header_text.splitlines()) == 1
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
        for _ in range(60):
            if not app._is_streaming:
                break
            await pilot.pause(0.05)
        assert not app._is_streaming

        assistant = session.state.messages[-1]
        assert assistant.content == "这是正式回答"
        assert [entry.kind for entry in assistant.trace.entries] == ["thinking", "text"]

        trace_widget = list(app.query(ThinkingTraceWidget))[-1]
        assert trace_widget.collapsed is True
        trace_widget.toggle_collapsed()
        await pilot.pause(0.05)
        assert trace_widget.collapsed is False
        rendered = trace_widget.query_one(".thinking-trace-header", Static).render()
        assert "先整理一下思路" in rendered.plain
        assert "思考：" not in rendered.plain

        formatted = _format_trace_entries(assistant.trace.entries)
        assert isinstance(formatted, Text)
        segments = {
            (formatted.plain[span.start : span.end], span.style)
            for span in formatted.spans
        }
        assert ("先整理一下思路", "#929aa7") in segments


@pytest.mark.asyncio
async def test_tui_interleaves_tool_flow_and_separate_thinking_segments(
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
            "text",
        ]

        trace_widget = list(app.query(ThinkingTraceWidget))[0]
        trace_widget.toggle_collapsed()
        await pilot.pause(0.05)
        rendered = trace_widget.query_one(".thinking-trace-header", Static).render()
        assert "先调用工具" in rendered.plain
        assert "echo_tool" not in rendered.plain
        activity = list(app.query(ToolActivityWidget))[-1]
        call = activity.query_one(ToolCallWidget)
        call.toggle_collapsed()
        tool_rendered = call.query_one(".tool-call-body", Static).render()
        assert '"value": "hello"' in tool_rendered.plain
        assert "执行成功: hello" in tool_rendered.plain
        assert "工具结果: hello" in tool_rendered.plain
        timeline = activity.parent
        assert [type(widget) for widget in timeline.children] == [ThinkingTraceWidget, ToolActivityWidget, ThinkingTraceWidget, Static]

        formatted = _format_trace_entries(assistant.trace.entries)
        assert isinstance(formatted, Text)
        segments = {
            (formatted.plain[span.start : span.end], span.style)
            for span in formatted.spans
        }
        assert ("先调用工具", "#929aa7") in segments
        assert ("● echo_tool(value=hello)", "#83b6f6") in segments
        assert ("✓ 执行成功: hello", "#93c7a0") in segments
        assert ("再整理一下", "#929aa7") in segments


@pytest.mark.asyncio
async def test_thinking_trace_preserves_user_expansion_until_task_completes(
    openai_provider_config,
    ui_config,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                (StreamEvent(kind="thinking_delta", text="thinking"), 0.3),
                StreamEvent(kind="text_delta", text="final answer"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)

    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "demo prompt")
        await pilot.pause(0.05)

        trace_widget = list(app.query(ThinkingTraceWidget))[-1]
        assert trace_widget.collapsed is False
        trace_widget.toggle_collapsed()
        assert trace_widget.collapsed is True
        trace_widget.toggle_collapsed()
        assert trace_widget.collapsed is False

        for _ in range(60):
            if not app._is_streaming:
                break
            await pilot.pause(0.05)
        assert not app._is_streaming

        trace_widget = list(app.query(ThinkingTraceWidget))[-1]
        assert trace_widget.collapsed is True
        assert session.state.messages[-1].trace.collapsed is True
        trace_widget.toggle_collapsed()
        await app._sync_message_widget(session.state.messages[-1].id)
        assert trace_widget.collapsed is False


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
