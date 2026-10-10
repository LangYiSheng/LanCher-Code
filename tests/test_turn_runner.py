from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lancher_code.errors import ProviderPromptTooLongError, ProviderRequestError
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolCallChunk, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import MAX_TOOL_LOOPS, TurnRunner

DelayedEvent = tuple[StreamEvent, float]


def _summary_text() -> str:
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
    return "<summary>" + "\n".join(f"## {heading}\n内容" for heading in headings) + "</summary>"


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
    def resource_claims(self, arguments, context):
        # 测试工具只构造字符串，显式声明不访问共享资源。
        return ()

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
    session = SessionController(openai_provider_config, cwd=tmp_path)
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
        "turn_completed",
    ]
    assert events[-1].message is not None
    assert events[-1].message.content == "直接回答"
    assert len(provider.requests) == 1
    assert [message.role for message in session.transcript] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_turn_runner_emergency_compacts_and_retries_once(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            ProviderPromptTooLongError("maximum context length exceeded"),
            [StreamEvent(kind="text_delta", text=_summary_text()), StreamEvent(kind="message_end")],
            [
                StreamEvent(kind="text_delta", text="恢复成功"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=10, output_tokens=2)),
            ],
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("继续任务")]

    assert events[-1].kind == "turn_completed"
    assert events[-1].message is not None and events[-1].message.content == "恢复成功"
    assert len(provider.requests) == 3
    assert provider.requests[1].allow_tool_calls is False
    assert provider.requests[1].tools == []
    assert provider.requests[2].allow_tool_calls is True
    assert session.context_state.usage_anchor is not None


@pytest.mark.asyncio
async def test_manual_compact_does_not_create_display_message(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[
            [StreamEvent(kind="text_delta", text=_summary_text()), StreamEvent(kind="message_end")]
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)
    session.create_user_message("已有任务")
    saved_id = session.session_id
    message_count = len(session.state.messages)

    result = await runner.compact_context()

    assert result.before_tokens > 0
    assert len(session.state.messages) == message_count
    assert provider.requests[0].allow_tool_calls is False
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)
    assert restored.transcript[0].blocks[0].text == "以下内容是较早会话的压缩历史。"


@pytest.mark.asyncio
async def test_automatic_compaction_triggers_before_normal_request(openai_provider_config, tmp_path: Path) -> None:
    openai_provider_config.context_window = 34_000
    provider = FakeProvider(
        responses=[
            [StreamEvent(kind="text_delta", text=_summary_text()), StreamEvent(kind="message_end")],
            [
                StreamEvent(kind="text_delta", text="完成"),
                StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=20, output_tokens=2)),
            ],
        ]
    )
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    events = [event async for event in runner.run_user_turn("x" * 5_000)]

    assert events[-1].kind == "turn_completed"
    assert provider.requests[0].allow_tool_calls is False
    assert provider.requests[1].allow_tool_calls is True
    assert session.context_state.automatic_failure_count == 0


@pytest.mark.asyncio
async def test_automatic_compaction_circuit_breaker_persists_after_three_failures(
    openai_provider_config,
    tmp_path: Path,
) -> None:
    openai_provider_config.context_window = 34_000
    responses: list[list[StreamEvent | DelayedEvent] | Exception] = []
    for _ in range(3):
        responses.extend(
            [
                ProviderRequestError("summary failed"),
                [StreamEvent(kind="text_delta", text="继续"), StreamEvent(kind="message_end")],
            ]
        )
    responses.append([StreamEvent(kind="text_delta", text="熔断后继续"), StreamEvent(kind="message_end")])
    provider = FakeProvider(responses=responses)
    runner, session = _runner(provider, openai_provider_config, tmp_path)

    for index in range(4):
        _ = [event async for event in runner.run_user_turn(f"{index}" + "x" * 5_000)]

    assert session.context_state.automatic_failure_count == 3
    assert session.context_state.automatic_compaction_disabled is True
    assert len([request for request in provider.requests if not request.allow_tool_calls]) == 3


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
    registry.register_deferred_server("demo", title="Demo MCP", description="远程 Echo 服务")
    registry.register(DeferredEchoTool(), deferred_server_name="demo")
    session = SessionController(openai_provider_config)
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor)

    events = [event async for event in runner.run_user_turn("使用远程 echo")]

    assert events[-1].kind == "turn_completed"
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
    registry.register_deferred_server("demo", title="Demo MCP", description=None)
    registry.register(DeferredEchoTool(), deferred_server_name="demo")
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
    registry.register_deferred_server("demo", title="Demo MCP", description=None)
    registry.register(DeferredEchoTool(), deferred_server_name="demo")
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

    assert events[-1].kind == "turn_completed"
    assert len(provider.requests) == 2
    assert sum(event.kind == "tool_call_started" for event in events) == 2
    assert sum(event.kind == "tool_result_received" for event in events) == 2
    entries = session.state.messages[-1].trace.entries
    assert [entry.kind for entry in entries] == ["thinking", "tool_call", "tool_call", "tool_result", "tool_result", "text"]
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

    assert events[-1].kind == "turn_completed"
    assert len(provider.requests) == 3
    assert session.state.messages[-1].content == "终于答完"
    assert [entry.kind for entry in session.state.messages[-1].trace.entries] == [
        "thinking",
        "tool_call",
        "tool_result",
        "thinking",
        "tool_call",
        "tool_result",
        "text",
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

    assert events[-1].kind == "turn_completed"
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
    assert session.state.messages[-1].trace.entries[0].metadata["state"] == "cancelled"
    assert session.state.messages[-1].trace.entries[-1].kind == "notice"


@pytest.mark.asyncio
async def test_turn_runner_keeps_thinking_text_and_tool_batches_in_output_order(openai_provider_config, tmp_path: Path) -> None:
    provider = FakeProvider(responses=[
        [
            StreamEvent(kind="thinking_delta", text="检查入口"),
            StreamEvent(kind="text_delta", text="先定位配置"),
            StreamEvent(kind="thinking_delta", text="再查保存"),
            StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                call_index=0, provider_call_id="call-1", name_delta="echo_tool", arguments_delta='{"value":"a"}')),
            StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=2, output_tokens=3)),
        ],
        [
            StreamEvent(kind="text_delta", text="已经定位"),
            StreamEvent(kind="thinking_delta", text="核对结果"),
            StreamEvent(kind="text_delta", text="最终总结"),
            StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=4, output_tokens=5)),
        ],
    ])
    runner, session = _runner(provider, openai_provider_config, tmp_path)
    _ = [event async for event in runner.run_user_turn("检查配置")]
    entries = session.state.messages[-1].trace.entries
    assert [entry.kind for entry in entries] == [
        "thinking", "text", "thinking", "tool_call", "tool_result", "text", "thinking", "text",
    ]
    assert [entry.text for entry in entries if entry.kind in {"thinking", "text"}] == [
        "检查入口", "先定位配置", "再查保存", "已经定位", "核对结果", "最终总结",
    ]
    assert all(entry.metadata["state"] == "complete" for entry in entries)
    assert session.total_usage().input_tokens == 6
    assert session.total_usage().output_tokens == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_after_first", [False, True])
async def test_turn_runner_reports_each_parallel_result_and_never_duplicates_on_cancel(
    openai_provider_config, tmp_path: Path, cancel_after_first: bool,
) -> None:
    release = asyncio.Event()

    class ControlledEcho(EchoTool):
        async def execute(self, arguments, context):
            if arguments["value"] == "slow":
                await release.wait()
            return await super().execute(arguments, context)

    provider = FakeProvider(responses=[
        [
            StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                call_index=0, provider_call_id="slow", name_delta="echo_tool", arguments_delta='{"value":"slow"}')),
            StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                call_index=1, provider_call_id="fast", name_delta="echo_tool", arguments_delta='{"value":"fast"}')),
            StreamEvent(kind="message_end"),
        ],
        [StreamEvent(kind="text_delta", text="完成"), StreamEvent(kind="message_end")],
    ])
    registry = ToolRegistry()
    registry.register(ControlledEcho())
    session = SessionController(openai_provider_config, cwd=tmp_path)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=tmp_path))
    first_result_observed = False

    async def collect():
        nonlocal first_result_observed
        async for event in runner.run_user_turn("并行读取"):
            if event.kind == "tool_result_received" and event.tool_result.call_id == "fast":
                first_result_observed = True
                call_entries = [entry for entry in event.message.trace.entries if entry.kind == "tool_call"]
                assert [entry.metadata["state"] for entry in call_entries] == ["running", "complete"]
                assert call_entries[0].metadata["group_id"] == call_entries[1].metadata["group_id"]
                assert len(provider.requests) == 1
                if cancel_after_first:
                    runner.cancel_active_turn()
                else:
                    release.set()

    await asyncio.wait_for(collect(), 3)
    assert first_result_observed
    results = [entry for entry in session.state.messages[-1].trace.entries if entry.kind == "tool_result"]
    assert [entry.call_id for entry in results] == ["fast", "slow"]
    assert results[0].ok is True
    assert results[0].metadata["started"] is True
    assert results[1].metadata["started"] is True
    assert results[1].metadata["state"] == ("cancelled" if cancel_after_first else "complete")
    protocol_results = [block for item in session.transcript for block in item.blocks if block.kind == "tool_result"]
    assert sorted(block.call_id for block in protocol_results) == ["fast", "slow"]
    assert len(protocol_results) == 2


@pytest.mark.asyncio
async def test_tool_argument_stream_notifies_thinking_finished_before_arguments_complete(openai_provider_config, tmp_path):
    release_arguments = asyncio.Event()

    class GatedProvider(FakeProvider):
        async def stream_chat(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield StreamEvent(kind="thinking_delta", text="先读取入口")
                yield StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                    call_index=0, provider_call_id="read", name_delta="echo_tool"))
                # 参数尚未输出时，界面就应收到思考结束的刷新通知。
                await release_arguments.wait()
                yield StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                    call_index=0, arguments_delta='{"value":'))
                yield StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                    call_index=0, arguments_delta='"done"}'))
            else:
                yield StreamEvent(kind="text_delta", text="完成")
            yield StreamEvent(kind="message_end")

    provider = GatedProvider(responses=[])
    runner, session = _runner(provider, openai_provider_config, tmp_path)
    notifications = []

    async def collect():
        async for event in runner.run_user_turn("读取入口"):
            if event.kind == "progress_updated" and event.progress_message == "正在准备工具调用":
                notifications.append(event)
                assert not release_arguments.is_set()
                assert [entry.kind for entry in event.message.trace.entries] == ["thinking"]
                assert event.message.trace.entries[0].metadata["state"] == "complete"
                assert session.state.messages[-1].status == "streaming"
                release_arguments.set()

    await asyncio.wait_for(collect(), 3)
    assert len(notifications) == 1
    assert len(provider.requests) == 2
    assert session.state.messages[-1].status == "complete"
