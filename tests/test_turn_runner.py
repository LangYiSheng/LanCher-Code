from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lancher_code.errors import ProviderRequestError
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolCallChunk, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import MAX_TOOL_LOOPS, TurnRunner


class FakeProvider:
    def __init__(self, responses: list[list[StreamEvent] | Exception]) -> None:
        self._responses = responses
        self.requests: list[ChatRequest] = []

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        current = self._responses.pop(0)
        if isinstance(current, Exception):
            raise current
        for event in current:
            yield event


class EchoTool:
    @property
    def definition(self):
        return ToolDefinition(name="echo_tool", description="echo", input_schema={"type": "object"})

    async def execute(self, arguments, context):
        return ToolExecutionResult(
            call_id="",
            tool_name=self.definition.name,
            ok=True,
            payload={"content": f"工具结果: {arguments['value']}"},
            summary=f"echo ok: {arguments['value']}",
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
        "assistant_trace_updated",
        "assistant_message_completed",
    ]
    assert events[-1].message is not None
    assert events[-1].message.content == "直接回答"
    assert len(provider.requests) == 1
    assert [message.role for message in session.transcript] == ["system", "user", "assistant"]


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
