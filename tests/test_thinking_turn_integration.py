from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from lancher_code.context_management import SUMMARY_HEADINGS
from lancher_code.errors import ContextCompactionError
from lancher_code.models import ToolDefinition, ToolExecutionResult
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.session import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import TurnRunner


class RecordingEchoTool:
    def __init__(self) -> None:
        self.executed: list[dict[str, object]] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo_tool", description="返回输入，记录实际执行次数",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                          "required": ["value"]},
        )

    def resource_claims(self, arguments, context):
        return ()

    async def execute(self, arguments, context) -> ToolExecutionResult:
        self.executed.append(deepcopy(arguments))
        return ToolExecutionResult(
            call_id="", tool_name="echo_tool", content=f"echo: {arguments['value']}",
            is_error=False, summary="已执行 echo",
        )


def _sse(events: list[dict[str, object]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ).encode("utf-8")


def _response(
    label: str,
    calls: tuple[tuple[str, str], ...] = (),
    *,
    stop_reason: str | None = None,
    complete: bool = True,
) -> tuple[bytes, list[dict[str, object]]]:
    """真实 SSE 同时包含起始内容、增量、签名和不可读思考块。"""
    expected: list[dict[str, object]] = [
        {"type": "thinking", "thinking": f"{label}起始思考，继续思考",
         "signature": f"{label}-signature-start-middle-end"},
        {"type": "redacted_thinking", "data": f"{label}-opaque-data"},
        {"type": "text", "text": f"{label}正文，继续正文"},
    ]
    events: list[dict[str, object]] = [
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 32, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "thinking", "thinking": f"{label}起始思考",
            "signature": f"{label}-signature-start",
        }},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "，继续思考"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "-middle"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "-end"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": expected[1]},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "text", "text": f"{label}正文"}},
        {"type": "content_block_delta", "index": 2,
         "delta": {"type": "text_delta", "text": "，继续正文"}},
        {"type": "content_block_stop", "index": 2},
    ]
    for index, (call_id, arguments) in enumerate(calls, start=3):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = {"INVALID_JSON": arguments}
        expected.append({"type": "tool_use", "id": call_id, "name": "echo_tool", "input": parsed})
        events.extend([
            {"type": "content_block_start", "index": index,
             "content_block": {"type": "tool_use", "id": call_id, "name": "echo_tool", "input": {}}},
            {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta", "partial_json": arguments[:7]}},
            {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta", "partial_json": arguments[7:]}},
            {"type": "content_block_stop", "index": index},
        ])
    events.append({"type": "message_delta", "delta": {
        "stop_reason": stop_reason or ("tool_use" if calls else "end_turn"),
    }, "usage": {"output_tokens": 64}})
    if complete:
        events.append({"type": "message_stop"})
    return _sse(events), expected


@asynccontextmanager
async def _harness(config, tmp_path: Path, responses: list[bytes | httpx.AsyncByteStream]):
    requests: list[dict[str, object]] = []
    remaining = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert remaining, "模型不应额外发起请求"
        current = remaining.pop(0)
        if isinstance(current, bytes):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=current)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=current)

    transport = httpx.MockTransport(handle)
    provider = ClaudeProvider(config, client_factory=lambda: httpx.AsyncClient(transport=transport))
    tool = RecordingEchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    session = SessionController(config, cwd=tmp_path, initial_permission_policy="bypass")
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    runner = TurnRunner(provider, session, registry, executor)
    try:
        yield runner, session, tool, requests, provider
    finally:
        await runner.shutdown()
        session.close()


def _assistant_content(payload: dict[str, object]) -> list[list[dict[str, object]]]:
    return [message["content"] for message in payload["messages"] if message["role"] == "assistant"]


def _tool_results(payload: dict[str, object]) -> list[dict[str, object]]:
    return [block for message in payload["messages"] for block in message["content"]
            if block["type"] == "tool_result"]


@pytest.mark.asyncio
async def test_claude_tool_loops_preserve_each_thinking_exchange_and_resume(
    claude_provider_config, tmp_path: Path,
) -> None:
    first, first_expected = _response("第一轮", (("original-first", '{"value":"one"}'),))
    second, second_expected = _response("第二轮", (("original-second", '{"value":"two"}'),))
    final, final_expected = _response("最终回答")
    async with _harness(claude_provider_config, tmp_path, [first, second, final]) as (
        runner, session, tool, requests, provider,
    ):
        events = [event async for event in runner.run_user_turn("请连续调用两次工具")]
        assert events[-1].kind == "turn_completed"
        assert tool.executed == [{"value": "one"}, {"value": "two"}]
        assert len(requests) == 3
        assert _assistant_content(requests[1]) == [first_expected]
        assert _assistant_content(requests[2]) == [first_expected, second_expected]
        assert [result["tool_use_id"] for result in _tool_results(requests[2])] == [
            "original-first", "original-second",
        ]
        expected_transcript = deepcopy(session.transcript)
        assert [message.role for message in expected_transcript] == [
            "user", "assistant", "tool", "assistant", "tool", "assistant",
        ]
        assert provider._serialize_message(expected_transcript[-1])["content"] == final_expected
        assert sum(block.text == "最终回答正文，继续正文"
                   for message in expected_transcript for block in message.blocks) == 1
        session_id = session.session_id

    restored = SessionController(claude_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        assert restored.transcript == expected_transcript
        payload = provider._build_payload(restored.build_request([], allow_tool_calls=False))
        assert _assistant_content(payload) == [first_expected, second_expected, final_expected]
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_malformed_tool_batch_keeps_original_identity_and_does_not_execute_any_call(
    claude_provider_config, tmp_path: Path,
) -> None:
    raw = '{"value":"' + "这是一段被截断的网页正文" * 300
    broken, expected = _response("坏参数", (
        ("valid-original", '{"value":"must-not-execute"}'), ("invalid-original", raw),
    ))
    final, _ = _response("重新说明")
    async with _harness(claude_provider_config, tmp_path, [broken, final]) as (
        runner, session, tool, requests, _provider,
    ):
        events = [event async for event in runner.run_user_turn("写入文件")]
        assert events[-1].kind == "turn_completed"
        assert tool.executed == []
        assert len(requests) == 2
        assert _assistant_content(requests[1]) == [expected]
        assert expected[-1]["input"] == {"INVALID_JSON": raw}
        results = _tool_results(requests[1])
        assert [result["tool_use_id"] for result in results] == ["valid-original", "invalid-original"]
        assert all(result["is_error"] is True and len(result["content"]) < 512 for result in results)
        assert all(raw not in result["content"] and "本批" in result["content"] for result in results)
        assert all(block.name != "tool_call_parser" for message in session.transcript for block in message.blocks)
        assert {event.tool_result.error_code for event in events if event.tool_result is not None} == {
            "tool_batch_not_executed", "tool_call_parse_error",
        }


@pytest.mark.asyncio
async def test_max_tokens_stops_even_a_syntactically_valid_tool_batch(
    claude_provider_config, tmp_path: Path,
) -> None:
    truncated, expected = _response("输出到顶", (("original-capped", '{"value":"safe-json"}'),),
                                    stop_reason="max_tokens")
    final, _ = _response("分段重试")
    async with _harness(claude_provider_config, tmp_path, [truncated, final]) as (
        runner, _session, tool, requests, _provider,
    ):
        events = [event async for event in runner.run_user_turn("生成大文件")]
        assert events[-1].kind == "turn_completed"
        assert tool.executed == []
        assert _assistant_content(requests[1]) == [expected]
        results = _tool_results(requests[1])
        assert len(results) == 1 and results[0]["tool_use_id"] == "original-capped"
        assert results[0]["is_error"] is True
        assert "输出上限" in results[0]["content"]


@pytest.mark.asyncio
async def test_eof_without_message_stop_does_not_execute_or_commit_the_partial_exchange(
    claude_provider_config, tmp_path: Path,
) -> None:
    incomplete, _ = _response("提前断流", (("original-unfinished", '{"value":"no-execution"}'),),
                               complete=False)
    async with _harness(claude_provider_config, tmp_path, [incomplete]) as (
        runner, session, tool, requests, _provider,
    ):
        events = [event async for event in runner.run_user_turn("运行工具")]
        assert events[-1].kind == "turn_failed"
        assert "未收到完整响应" in events[-1].error_text
        assert tool.executed == []
        assert len(requests) == 1
        assert [message.role for message in session.transcript] == ["user"]
        assert all(event.tool_result is None for event in events)
        assert len(session.state.request_usage) == 1
        record = next(iter(session.state.request_usage.values()))
        assert record["status"] == "incomplete"


class InterruptedSSE(httpx.AsyncByteStream):
    def __init__(self, partial: bytes) -> None:
        self.partial = partial

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.partial
        raise httpx.RemoteProtocolError("peer closed connection without completing the response")


@pytest.mark.asyncio
async def test_compact_rejects_closed_nine_section_summary_when_sse_has_no_message_stop(
    claude_provider_config, tmp_path: Path,
) -> None:
    summary = "<summary>" + "\n".join(f"## {heading}\n已整理。" for heading in SUMMARY_HEADINGS) + "</summary>"
    split = len(summary) // 2
    incomplete = _sse([
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 128, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": summary[:split]}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": summary[split:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 64}},
    ])
    async with _harness(claude_provider_config, tmp_path, [incomplete]) as (
        _runner, session, tool, requests, provider,
    ):
        session.create_user_message("旧任务材料" + "x" * 50_000)
        message = session.create_assistant_message()
        session.append_message_content(message.id, "材料已记录。")
        session.complete_message(message.id)
        session.create_user_message("继续当前任务")
        original_transcript = deepcopy(session.transcript)

        with pytest.raises(ContextCompactionError, match="摘要响应未正常结束"):
            await session.compact_context(provider=provider, visible_tools=[])

        assert session.transcript == original_transcript
        assert tool.executed == [] and len(requests) == 1
        assert requests[0]["thinking"] == {"type": "disabled"}
        assert len(session.state.request_usage) == 1
        record = next(iter(session.state.request_usage.values()))
        assert record["status"] == "incomplete"
        assert record["purpose"] == "compaction"
        assert len(session.state.compaction_activities) == 1
        activity = next(iter(session.state.compaction_activities.values()))
        assert activity.status == "failed"
        assert "摘要响应未正常结束" in activity.error_text


@pytest.mark.asyncio
async def test_transport_eof_does_not_execute_tools_and_records_failed_attempt(
    claude_provider_config, tmp_path: Path,
) -> None:
    incomplete, _ = _response("网络中断", (("original-network", '{"value":"no-execution"}'),),
                               complete=False)
    async with _harness(claude_provider_config, tmp_path, [InterruptedSSE(incomplete)]) as (
        runner, session, tool, requests, _provider,
    ):
        events = [event async for event in runner.run_user_turn("运行工具")]
        assert events[-1].kind == "turn_failed"
        assert tool.executed == []
        assert len(requests) == 1
        assert [message.role for message in session.transcript] == ["user"]
        record = next(iter(session.state.request_usage.values()))
        assert record["status"] == "failed"


@pytest.mark.asyncio
async def test_plain_final_answer_saves_signed_thinking_once(
    claude_provider_config, tmp_path: Path,
) -> None:
    final, expected = _response("直接回答")
    async with _harness(claude_provider_config, tmp_path, [final]) as (
        runner, session, tool, requests, provider,
    ):
        events = [event async for event in runner.run_user_turn("你好")]
        assert events[-1].kind == "turn_completed"
        assert events[-1].message.content == "直接回答正文，继续正文"
        assert tool.executed == [] and len(requests) == 1
        assert [message.role for message in session.transcript] == ["user", "assistant"]
        assert provider._serialize_message(session.transcript[-1])["content"] == expected
        assert len([entry for entry in events[-1].message.trace.entries if entry.kind == "thinking"]) == 1
        assert "signature" not in events[-1].message.trace.entries[0].text
