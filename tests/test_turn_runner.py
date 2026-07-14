from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lancher_code.errors import ProviderRequestError
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolCallChunk, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import MAX_TOOL_LOOPS, TurnRunner

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
            summary=f"echo ok: {arguments['value']}",
        )


class DeferredEchoTool(EchoTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="mcp__demo__echo",
            description="来自 demo Server 的延迟 echo",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
            should_defer=True,
        )


class DiscoverTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="tool_search", description="发现工具", input_schema={"type": "object"})

    async def execute(self, arguments, context) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id="",
            tool_name="tool_search",
            content="已发现",
            metadata={"discovered_tool_names": ["mcp__demo__echo"]},
            summary="已发现工具",
        )


def _runner(provider: FakeProvider, openai_provider_config, tmp_path: Path) -> tuple[TurnRunner, SessionController]:
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = SessionController(openai_provider_config)
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    return TurnRunner(provider, session, registry, executor), session


@pytest.mark.asyncio
async def test_turn_runner_completes_plain_text_turn(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="直接回答"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=2, output_tokens=3)),
            ]
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("你好")]

    assert [event.kind for event in events] == [
        "user_message_created",
        "assistant_message_started",
        "progress_updated",
        "progress_updated",
        "assistant_text_delta",
        "usage_updated",
        "assistant_message_completed",
    ]
    assert events[-1].message is not None
    assert events[-1].message.content == "直接回答"
    assert len(provider.requests) == 1
    assert [message.role for message in session.transcript] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_turn_runner_adds_discovered_schema_only_to_next_loop(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-search", name_delta="tool_search")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"query":"echo"}')),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="已加载"),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(DiscoverTool())
    registry.register(DeferredEchoTool())
    session = SessionController(openai_provider_config)
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor)

    events = [event async for event in runner.run_user_turn("使用远程 echo")]

    assert events[-1].kind == "assistant_message_completed"
    assert [tool.name for tool in provider.requests[0].tools] == ["tool_search"]
    assert [tool.name for tool in provider.requests[1].tools] == ["tool_search", "mcp__demo__echo"]
    assert "mcp__demo__echo" in provider.requests[0].system[-1]


@pytest.mark.asyncio
async def test_turn_runner_resets_discovered_tools_for_next_user_turn(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-search", name_delta="tool_search")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"query":"echo"}')),
                StreamEvent(kind="message_end"),
            ],
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="完成"), StreamEvent(kind="message_end")],
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="新一轮"), StreamEvent(kind="message_end")],
        ]
    )
    registry = ToolRegistry()
    registry.register(DiscoverTool())
    registry.register(DeferredEchoTool())
    session = SessionController(openai_provider_config)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=tmp_path))

    _ = [event async for event in runner.run_user_turn("第一轮")]
    _ = [event async for event in runner.run_user_turn("第二轮")]

    assert [tool.name for tool in provider.requests[2].tools] == ["tool_search"]


@pytest.mark.asyncio
async def test_turn_runner_rejects_direct_call_to_undiscovered_tool(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-hidden", name_delta="mcp__demo__echo")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"x"}')),
                StreamEvent(kind="message_end"),
            ],
            [StreamEvent(kind="message_start"), StreamEvent(kind="text_delta", text="先搜索"), StreamEvent(kind="message_end")],
        ]
    )
    registry = ToolRegistry()
    registry.register(DiscoverTool())
    registry.register(DeferredEchoTool())
    session = SessionController(openai_provider_config)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=tmp_path))

    events = [event async for event in runner.run_user_turn("直接调用隐藏工具")]
    result_events = [event for event in events if event.kind == "tool_result_received"]

    assert result_events[0].tool_result is not None
    assert result_events[0].tool_result.error_code == "tool_not_found"
    assert result_events[0].tool_result.metadata["requires_tool_search"] is True


@pytest.mark.asyncio
async def test_turn_runner_executes_multiple_tool_calls_in_one_reply(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="thinking_delta", text="先调两个工具"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"a"}')),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=1, provider_call_id="call-2", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=1, arguments_delta='{"value":"b"}')),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=2, output_tokens=1)),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="最终回答"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=3, output_tokens=4)),
            ],
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("帮我执行工具")]

    assert events[-1].kind == "assistant_message_completed"
    assert len(provider.requests) == 2
    assert sum(event.kind == "tool_call_started" for event in events) == 2
    assert sum(event.kind == "tool_result_received" for event in events) == 2
    entries = session.state.messages[-1].trace.entries
    assert [entry.kind for entry in entries] == ["thinking", "tool_call", "tool_call", "tool_result", "tool_result"]
    assert session.state.messages[-1].content == "最终回答"


@pytest.mark.asyncio
async def test_turn_runner_loops_until_text_after_multiple_batches(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="thinking_delta", text="第一轮"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"a"}')),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="thinking_delta", text="第二轮"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-2", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"b"}')),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="终于答完"),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("多轮工具")]

    assert events[-1].kind == "assistant_message_completed"
    assert len(provider.requests) == 3
    assert session.state.messages[-1].content == "终于答完"
    assert [entry.kind for entry in session.state.messages[-1].trace.entries] == [
        "thinking",
        "tool_call",
        "tool_result",
        "thinking",
        "tool_call",
        "tool_result",
    ]


@pytest.mark.asyncio
async def test_turn_runner_records_parser_error_and_continues(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":')),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="解析失败后的最终说明"),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("坏参数")]

    assert events[-1].kind == "assistant_message_completed"
    assert session.state.messages[-1].content == "解析失败后的最终说明"
    assert any(entry.kind == "tool_result" and entry.ok is False for entry in session.state.messages[-1].trace.entries)


@pytest.mark.asyncio
async def test_turn_runner_stops_on_loop_limit(openai_provider_config, tmp_path: Path) -> None:
    responses: list[list[StreamEvent]] = []
    for index in range(MAX_TOOL_LOOPS):
        responses.append(
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id=f"call-{index}", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"x"}')),
                StreamEvent(kind="message_end"),
            ]
        )
    provider = FakeProvider(responses=responses)
    runner, _session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("循环太多")]

    assert events[-1].kind == "turn_failed"
    assert events[-1].error_text is not None
    assert "达到上限" in events[-1].error_text


@pytest.mark.asyncio
async def test_turn_runner_reports_provider_error(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(responses=[ProviderRequestError("网络失败")])
    runner, _session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("失败")]

    assert events[-1].kind == "turn_failed"
    assert events[-1].error_text == "网络失败"


@pytest.mark.asyncio
async def test_turn_runner_accumulates_usage_after_each_model_call(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"a"}')),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=2, cached_input_tokens=1, output_tokens=3)),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="text_delta", text="最终回答"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=5, cached_input_tokens=4, output_tokens=7)),
            ],
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("多次计费")]

    usage_updates = [event for event in events if event.kind == "usage_updated"]
    assert usage_updates[0].usage.input_tokens == 2
    assert usage_updates[0].usage.cached_input_tokens == 1
    assert usage_updates[0].usage.output_tokens == 3
    assert usage_updates[1].usage.input_tokens == 7
    assert usage_updates[1].usage.cached_input_tokens == 5
    assert usage_updates[1].usage.output_tokens == 10
    assert session.state.messages[-1].usage.input_tokens == 7
    assert session.state.messages[-1].usage.cached_input_tokens == 5
    assert session.state.messages[-1].usage.output_tokens == 10


@pytest.mark.asyncio
async def test_turn_runner_keeps_accumulated_usage_on_failed_turn(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"a"}')),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=4, cached_input_tokens=3, output_tokens=6)),
            ],
            ProviderRequestError("网络失败"),
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("失败但要计费")]

    assert events[-1].kind == "turn_failed"
    assert session.state.messages[-1].usage.input_tokens == 4
    assert session.state.messages[-1].usage.cached_input_tokens == 3
    assert session.state.messages[-1].usage.output_tokens == 6


@pytest.mark.asyncio
async def test_turn_runner_respects_custom_loop_limit(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"x"}')),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-2", name_delta="echo_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta='{"value":"x"}')),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(EchoTool())
    session = SessionController(openai_provider_config)
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor, max_tool_loops=1)

    events = [event async for event in runner.run_user_turn("限制一轮")]

    assert events[-1].kind == "turn_failed"
    assert events[-1].error_text is not None
    assert "1 次" in events[-1].error_text


@pytest.mark.asyncio
async def test_turn_runner_stops_after_consecutive_unknown_tools(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="missing_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta="{}")),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-2", name_delta="missing_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta="{}")),
                StreamEvent(kind="message_end"),
            ],
            [
                StreamEvent(kind="message_start"),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, provider_call_id="call-3", name_delta="missing_tool")),
                StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(call_index=0, arguments_delta="{}")),
                StreamEvent(kind="message_end"),
            ],
        ]
    )
    registry = ToolRegistry()
    session = SessionController(openai_provider_config)
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor, unknown_tool_streak_limit=3)

    events = [event async for event in runner.run_user_turn("连续未知工具")]

    assert events[-1].kind == "turn_failed"
    assert events[-1].error_text is not None
    assert "连续请求未知工具" in events[-1].error_text


@pytest.mark.asyncio
async def test_turn_runner_can_cancel_active_turn(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [
                StreamEvent(kind="message_start"),
                (StreamEvent(kind="text_delta", text="先来一点"), 0.5),
                StreamEvent(kind="text_delta", text="后面的内容"),
                StreamEvent(kind="message_end"),
            ]
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    async def collect_events():
        return [event async for event in runner.run_user_turn("取消一下")]

    task = asyncio.create_task(collect_events())
    await asyncio.sleep(0.1)
    assert runner.cancel_active_turn() is True
    events = await task

    assert events[-1].kind == "turn_cancelled"
    assert session.state.messages[-1].status == "cancelled"
