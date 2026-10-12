"""真实 SSE 验证输出耗尽、续接、断流和会话恢复的完整链路。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from lancher_code.agent.runner import TurnRunner
from lancher_code.config.models import RuntimeConfig
from lancher_code.contracts.messages import ContentBlock, StreamEvent
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.responses import ResponsesProvider
from lancher_code.sessions.controller import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry


def _chat_round(*, text="", thinking="", reason="stop", complete=True):
    chunks = []
    if thinking:
        chunks.append({"choices": [{"delta": {"reasoning_content": thinking}}]})
    if text:
        chunks.append({"choices": [{"delta": {"content": text}}]})
    chunks.append({"choices": [{"delta": {}, "finish_reason": reason}],
                   "usage": {"prompt_tokens": 40, "completion_tokens": 12,
                             "prompt_tokens_details": {"cached_tokens": 0}}})
    return ("".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
            + ("data: [DONE]\n\n" if complete else "")).encode()


@asynccontextmanager
async def _harness(config, tmp_path, responses, provider_type=OpenAIProvider):
    requests = []
    remaining = list(responses)

    def handle(request):
        requests.append(json.loads(request.content))
        assert remaining, "模型不应额外发起请求"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=remaining.pop(0))

    provider = provider_type(config, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    registry = ToolRegistry()
    session = SessionController(config, cwd=tmp_path)
    executor = ToolExecutor(registry, cwd=tmp_path)
    runner = TurnRunner(provider, session, registry, executor)
    try:
        yield runner, session, requests
    finally:
        await runner.shutdown()
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [dict(thinking="还在分析问题"), dict(text="第一段")])
async def test_output_limit_continues_with_warning_and_preserves_history(openai_provider_config, tmp_path, initial):
    async with _harness(openai_provider_config, tmp_path, [
        _chat_round(**initial, reason="length"), _chat_round(text="后续答案"),
    ]) as (runner, session, requests):
        events = [event async for event in runner.run_user_turn("完整处理任务")]
        assert events[-1].kind == "turn_completed"
        assert len(requests) == 2 and requests[0]["max_tokens"] == 32768
        assert len([message for message in session.state.messages if message.role == "user"]) == 1
        message = session.state.messages[-1]
        assert message.content == initial.get("text", "") + "后续答案"
        notices = [entry for entry in message.trace.entries if entry.kind == "notice"]
        assert len(notices) == 1 and notices[0].metadata["severity"] == "warning"
        assert "自动续接" in notices[0].text
        assert "上一次响应达到了单次输出额度" in requests[1]["messages"][-1]["content"]
        records = list(session.state.request_usage.values())
        assert [record["status"] for record in records] == ["incomplete", "completed"]
        assert records[0]["stop_reason"] == "length" and records[0]["max_output_tokens"] == 32768
        saved_id = session.session_id
        session.resume_session(saved_id)
        assert session.state.messages[-1].content == initial.get("text", "") + "后续答案"
        assert any(item.response_stop_reason == "length" for item in session.transcript)


@pytest.mark.asyncio
async def test_length_then_eof_is_not_a_safe_continuation(openai_provider_config, tmp_path):
    async with _harness(openai_provider_config, tmp_path, [
        _chat_round(thinking="部分思考", reason="length", complete=False),
    ]) as (runner, session, requests):
        events = [event async for event in runner.run_user_turn("继续处理")]
        assert events[-1].kind == "turn_failed"
        assert "未收到完整响应" in events[-1].error_text
        assert len(requests) == 1
        assert not any(message.role == "assistant" for message in session.transcript)


@pytest.mark.asyncio
async def test_continuation_guard_reports_unfinished_and_keeps_partial_text(openai_provider_config, tmp_path):
    async with _harness(openai_provider_config, tmp_path, [
        _chat_round(text="已有内容", reason="length"), _chat_round(text="追加内容", reason="length"),
    ]) as (runner, session, requests):
        runner.apply_runtime_settings(RuntimeConfig(output_continuation_limit=1))
        events = [event async for event in runner.run_user_turn("长任务")]
        assert events[-1].kind == "turn_failed" and "已自动续接 1 次" in events[-1].error_text
        assert len(requests) == 2 and session.state.messages[-1].content == "已有内容追加内容"
        assert session.state.messages[-1].status == "error"
        assert {entry.metadata["severity"] for entry in session.state.messages[-1].trace.entries
                if entry.kind == "notice"} == {"warning", "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["content_filter", "aborted", "insufficient_system_resource"])
async def test_supplier_stopped_generation_keeps_body_and_fails(openai_provider_config, tmp_path, reason):
    async with _harness(openai_provider_config, tmp_path, [_chat_round(text="部分正文", reason=reason)]) as (
        runner, session, requests,
    ):
        events = [event async for event in runner.run_user_turn("处理任务")]
        assert events[-1].kind == "turn_failed"
        assert len(requests) == 1 and session.state.messages[-1].content == "部分正文"
        assert "本轮未完成" in events[-1].error_text
        record = next(iter(session.state.request_usage.values()))
        assert record["status"] == "incomplete" and record["stop_reason"] == reason


@pytest.mark.asyncio
async def test_responses_explicit_incomplete_continues_without_committing_partial_tool(openai_provider_config, tmp_path):
    def sse(events):
        return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode()

    partial = sse([
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "partial", "name": "write_file", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path":'},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "已分析，"},
        {"type": "response.incomplete", "response": {"status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"}, "usage": {"input_tokens": 40, "output_tokens": 12}}},
    ])
    final = sse([
        {"type": "response.output_text.delta", "output_index": 0, "delta": "继续完成。"},
        {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "继续完成。"}]}],
            "usage": {"input_tokens": 60, "output_tokens": 12}}},
    ])
    async with _harness(openai_provider_config, tmp_path, [partial, final], ResponsesProvider) as (
        runner, session, requests,
    ):
        events = [event async for event in runner.run_user_turn("写文件")]
        assert events[-1].kind == "turn_completed" and len(requests) == 2
        assert not any(event.kind == "tool_call_started" for event in events)
        assert not any(block.kind == "tool_use" for message in session.transcript for block in message.blocks)
        assert session.state.messages[-1].content == "已分析，继续完成。"


@pytest.mark.asyncio
async def test_only_thinking_with_natural_stop_is_unfinished(openai_provider_config, tmp_path):
    async with _harness(openai_provider_config, tmp_path, [_chat_round(thinking="只有思考")]) as (
        runner, session, requests,
    ):
        events = [event async for event in runner.run_user_turn("需要答案")]
        assert events[-1].kind == "turn_failed"
        assert "没有返回正文" in events[-1].error_text and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", None])
async def test_capped_tool_without_identity_is_not_executed_and_can_continue(openai_provider_config, tmp_path, reason):
    partial = ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"path":'}}]}}]}) + "\n\n"
        + "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": reason}],
                                 "usage": {"completion_tokens": 32768}})
        + "\n\ndata: [DONE]\n\n").encode()
    async with _harness(openai_provider_config, tmp_path, [partial, _chat_round(text="重新生成答案")]) as (
        runner, session, requests,
    ):
        events = [event async for event in runner.run_user_turn("继续当前任务")]
        assert events[-1].kind == "turn_completed" and len(requests) == 2
        assert not any(event.kind == "tool_call_started" for event in events)
        assert not any(block.kind == "tool_use" for message in session.transcript for block in message.blocks)
        record = next(iter(session.state.request_usage.values()))
        assert record["status"] == "incomplete" and record["stop_reason"] == reason


@pytest.mark.asyncio
async def test_claude_capped_unsigned_thinking_is_preserved_but_not_replayed_as_native(claude_provider_config, tmp_path):
    def response(block, reason):
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 40, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 12}},
            {"type": "message_stop"},
        ]
        return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode()

    async with _harness(claude_provider_config, tmp_path, [
        response({"type": "thinking", "thinking": "思考还未结束"}, "max_tokens"),
        response({"type": "text", "text": "给出答案"}, "end_turn"),
    ], ClaudeProvider) as (runner, session, requests):
        events = [event async for event in runner.run_user_turn("完成任务")]
        assert events[-1].kind == "turn_completed" and len(requests) == 2
        blocks = [block for message in requests[1]["messages"] if message["role"] == "assistant"
                  for block in message["content"]]
        assert all(block["type"] != "thinking" for block in blocks)
        assert any("未完成思考" in block.get("text", "") for block in blocks)
        original = next(message for message in session.transcript if message.response_stop_reason == "max_tokens")
        assert original.blocks[0].kind == "thinking" and original.blocks[0].text == "思考还未结束"


@pytest.mark.asyncio
async def test_custom_cap_without_snapshot_cannot_commit_tool_feedback(openai_provider_config, tmp_path):
    class Provider:
        count = 0

        async def stream_chat(self, request):
            self.count += 1
            if self.count == 1:
                yield StreamEvent(kind="text_delta", text="局部正文，")
                yield StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
                    call_index=0, provider_call_id="uncommitted", name_delta="write_file", arguments_delta="{}"))
                yield StreamEvent(kind="message_end", response_complete=True, stop_reason="length")
            else:
                yield StreamEvent(kind="text_delta", text="继续回答。")
                yield StreamEvent(kind="message_end", response_complete=True,
                                  assistant_blocks=[ContentBlock.text_block("继续回答。")], stop_reason="stop")

    provider = Provider()
    registry = ToolRegistry()
    session = SessionController(openai_provider_config, cwd=tmp_path)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=tmp_path))
    try:
        events = [event async for event in runner.run_user_turn("完成任务")]
        assert events[-1].kind == "turn_completed" and provider.count == 2
        assert not any(event.kind == "tool_call_started" for event in events)
        assert not any(block.kind in {"tool_use", "tool_result"} for message in session.transcript for block in message.blocks)
        partial = next(message for message in session.transcript if message.response_output_limited)
        assert partial.blocks[0].text == "局部正文，" and partial.response_stop_reason == "length"
    finally:
        await runner.shutdown()
        session.close()


@pytest.mark.asyncio
async def test_responses_incomplete_preserves_supplier_reason_and_body(openai_provider_config, tmp_path):
    partial = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in [
        {"type": "response.output_text.delta", "delta": "保留的正文"},
        {"type": "response.incomplete", "response": {"status": "incomplete",
            "incomplete_details": {"reason": "content_filter"}, "usage": {"input_tokens": 40, "output_tokens": 12}}},
    ]).encode()
    async with _harness(openai_provider_config, tmp_path, [partial], ResponsesProvider) as (runner, session, requests):
        events = [event async for event in runner.run_user_turn("处理任务")]
        assert events[-1].kind == "turn_failed" and "内容过滤" in events[-1].error_text
        assert session.state.messages[-1].content == "保留的正文" and len(requests) == 1
        record = next(iter(session.state.request_usage.values()))
        assert record["stop_reason"] == "content_filter" and record["status"] == "incomplete"
