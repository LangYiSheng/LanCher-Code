from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import httpx
import pytest

from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage
from lancher_code.contracts.tools import ToolDefinition, ToolPermissionMetadata
from lancher_code.errors import ProviderRequestError, ProviderResponseError
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.providers.responses import ResponsesProvider
from lancher_code.usage.ledger import RunUsageTracker


def _tool(name="mcp__docs__lookup", description="查找文档") -> ToolDefinition:
    return ToolDefinition(name=name, description=description,
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        permission=ToolPermissionMetadata(source="external", rule_key=name, display_name=name))


def _request() -> ChatRequest:
    return ChatRequest(model="test", system=["固定指令"], tools=[_tool("read_file")],
        messages=[ConversationMessage.text_message("user", "找文档")],
        experimental_mcp_tool_append=True,
        tool_updates=[{"at_message": 1, "additions": [asdict(_tool())], "removals": []}],
        prompt_cache_enabled=True)


def _sse(events) -> bytes:
    return "".join(f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                   for item in events).encode()


def _done(output=None, usage=None) -> dict:
    response = {"status": "completed", "output": output or []}
    if usage is not None:
        response["usage"] = usage
    return {"type": "response.completed", "response": response}


@pytest.mark.asyncio
async def test_claude_native_wire_keeps_baseline_and_projects_anchored_changes(claude_provider_config):
    captured = []
    request = _request()
    request.messages.append(ConversationMessage(role="tool", blocks=[
        ContentBlock.tool_result_block(call_id="first", text="a", is_error=False),
        ContentBlock.tool_result_block(call_id="second", text="b", is_error=False)]))
    request.tool_updates.append({"at_message": 2, "removals": [_tool().name],
                                 "additions": [asdict(_tool(description="更新后的定义"))]})
    original = deepcopy(request)

    def handler(raw):
        captured.append(json.loads(raw.content))
        assert raw.url.path == "/messages"
        assert raw.headers["anthropic-beta"] == "inline-tools-2026-09-15"
        return httpx.Response(200, content=_sse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 10,
             "cache_read_input_tokens": 30, "cache_creation_input_tokens": 4}}},
            {"type": "message_delta", "usage": {"output_tokens": 3}}, {"type": "message_stop"}]))

    tracker = RunUsageTracker()
    provider = ClaudeProvider(claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), usage_observer=tracker)
    events = [event async for event in provider.stream_chat(request)]
    payload = captured[0]
    assert payload["tools"] == [{"name": "read_file", "description": "查找文档", "input_schema": _tool().input_schema}]
    assert payload["cache_control"] == {"type": "ephemeral"}
    assert [message["role"] for message in payload["messages"]] == ["user", "system", "user", "system"]
    addition = payload["messages"][1]["content"][0]
    assert addition == {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {
        "name": _tool().name, "description": "查找文档", "input_schema": _tool().input_schema}}}
    assert payload["messages"][3]["content"][0] == {"type": "tool_removal", "tool": {
        "type": "tool_reference", "name": _tool().name}}
    assert "permission" not in json.dumps(addition)
    assert request.messages == original.messages and request.tools == original.tools and request.tool_updates == original.tool_updates
    assert events[-1].usage.input_tokens == 44
    assert tracker.snapshot().cached_input_tokens == 30
    assert tracker.snapshot().cache_creation_input_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,fixture,path", [
    (OpenAIProvider, "openai_provider_config", "/v1/chat/completions"),
    (ClaudeProvider, "claude_provider_config", "/messages")])
async def test_default_mode_does_not_opt_in_to_native_protocol_or_cache(request, provider_class, fixture, path):
    captured = []
    def handler(raw):
        captured.append(raw)
        assert raw.url.path == path
        assert "anthropic-beta" not in raw.headers
        payload = json.loads(raw.content)
        assert "cache_control" not in payload
        assert "input" not in payload
        assert "additional_tools" not in raw.content.decode()
        assert payload["tools"]
        body = b"data: [DONE]\n\n" if provider_class is OpenAIProvider else _sse([{"type": "message_stop"}])
        return httpx.Response(200, content=body)
    provider = provider_class(request.getfixturevalue(fixture),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _ = [event async for event in provider.stream_chat(ChatRequest(model="test", tools=[_tool()]))]
    assert len(captured) == 1


def test_native_mode_disables_calls_without_rewriting_baseline(claude_provider_config, openai_provider_config):
    request = _request()
    request.allow_tool_calls = False
    claude = ClaudeProvider(claude_provider_config)._build_payload(request)
    responses = OpenAIProvider(openai_provider_config)._build_payload(request)
    assert claude["tools"] and claude["tool_choice"] == {"type": "none"}
    assert responses["tools"] and responses["tool_choice"] == "none"
    assert responses["input"][-1]["type"] == "additional_tools"


@pytest.mark.parametrize("provider_class,fixture,choice", [
    (OpenAIProvider, "openai_provider_config", "none"), (ClaudeProvider, "claude_provider_config", {"type": "none"})])
def test_normal_mode_disables_calls_without_changing_schema_or_messages(request, provider_class, fixture, choice):
    provider = provider_class(request.getfixturevalue(fixture))
    outgoing = ChatRequest(model="test", tools=[_tool()], messages=[ConversationMessage.text_message("user", "任务")])
    first = provider._build_payload(outgoing)
    outgoing.allow_tool_calls = False
    second = provider._build_payload(outgoing)
    assert second["tools"] == first["tools"] and second["messages"] == first["messages"]
    assert second["tool_choice"] == choice


@pytest.mark.parametrize("change", [
    {"at_message": True}, {"at_message": 2}, {"at_message": -1},
    {"at_message": 1, "additions": [{"name": "bad", "input_schema": []}]},
    {"at_message": 1, "removals": [None]},
])
def test_invalid_native_history_is_rejected_before_http(claude_provider_config, change):
    request = _request()
    request.tool_updates = [change]
    with pytest.raises(ProviderRequestError, match="工具追加事件"):
        ClaudeProvider(claude_provider_config)._build_payload(request)


@pytest.mark.parametrize("change", [
    {"at_message": 1, "removals": ["read_file"]},
    {"at_message": 1, "additions": [asdict(_tool("read_file", "变化"))]},
])
def test_responses_requires_core_to_rebase_for_removal_or_replacement(openai_provider_config, change):
    request = _request()
    request.tool_updates = [change]
    with pytest.raises(ProviderRequestError, match="重建工具基线"):
        OpenAIProvider(openai_provider_config)._build_payload(request)


@pytest.mark.asyncio
async def test_responses_native_stream_round_trip_preserves_real_call_and_reasoning(openai_provider_config):
    captured = []
    reasoning = {"type": "reasoning", "id": "rs_real", "summary": [{"type": "summary_text", "text": "查文档"}],
                 "encrypted_content": "opaque-original"}
    function = {"type": "function_call", "id": "fc_real", "call_id": "call_real", "name": _tool().name,
                "arguments": '{"query":"MCP"}'}
    output = [reasoning, {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "已找到", "annotations": []}]}, function]
    def handler(raw):
        captured.append(json.loads(raw.content))
        assert raw.url.path == "/v1/responses"
        assert "messages" not in captured[-1] and "stream_options" not in captured[-1]
        if len(captured) == 2:
            return httpx.Response(200, content=_sse([_done()]))
        return httpx.Response(200, content=_sse([
            {"type": "response.created", "response": {"id": "resp-real"}},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "查文档"},
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "已找到"},
            {"type": "response.output_item.added", "output_index": 2, "item": {**function, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"query":'},
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"MCP"}'},
            _done(output, {"input_tokens": 100, "output_tokens": 12,
                "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 5}})]))
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), usage_observer=tracker)
    request = _request()
    events = [event async for event in provider.stream_chat(request)]
    blocks = events[-1].assistant_blocks
    assert events[-1].response_complete and blocks is not None
    assert [block.kind for block in blocks] == ["thinking", "text", "tool_use"]
    assert json.loads(blocks[0].data) == reasoning
    assert blocks[-1].call_id == "call_real" and blocks[-1].input == {"query": "MCP"}
    deltas = [event.tool_call_chunk for event in events if event.kind == "tool_call_delta"]
    assert "".join(item.name_delta for item in deltas) == _tool().name
    assert "".join(item.arguments_delta for item in deltas) == function["arguments"]
    assert tracker.snapshot().total_tokens == 112
    assert tracker.snapshot().cached_input_tokens == 80
    assert tracker.snapshot().reasoning_output_tokens == 5
    assert captured[0]["include"] == ["reasoning.encrypted_content"] and captured[0]["store"] is False
    assert captured[0]["input"][-1]["type"] == "additional_tools"
    request.messages.extend([ConversationMessage(role="assistant", blocks=blocks),
        ConversationMessage(role="tool", blocks=[ContentBlock.tool_result_block(call_id="call_real", text="文档", is_error=False)])])
    _ = [event async for event in provider.stream_chat(request)]
    assert reasoning in captured[1]["input"]
    assert captured[1]["input"][-1] == {"type": "function_call_output", "call_id": "call_real", "output": "文档"}
    assert captured[1]["input"][2] == captured[0]["input"][2]
    assert provider._serialize_message(ConversationMessage(role="assistant", blocks=blocks)).get("reasoning_content") is None


def test_responses_anchor_tracks_unified_messages_after_parallel_result_expansion(openai_provider_config):
    request = _request()
    request.messages.append(ConversationMessage(role="tool", blocks=[
        ContentBlock.tool_result_block(call_id="a", text="a", is_error=False),
        ContentBlock.tool_result_block(call_id="b", text="b", is_error=False)]))
    request.tool_updates[0]["at_message"] = 2
    payload = OpenAIProvider(openai_provider_config)._build_payload(request)
    assert [item.get("type") for item in payload["input"]] == [None, None, "function_call_output", "function_call_output", "additional_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [[], [{"type": "response.incomplete", "response": {
    "status": "incomplete", "output": [], "incomplete_details": {"reason": "max_output_tokens"},
    "usage": {"input_tokens": 20, "output_tokens": 5}}}]])
async def test_responses_unfinished_stream_never_exposes_executable_blocks(openai_provider_config, ending):
    body = _sse([{"type": "response.output_item.added", "output_index": 0,
        "item": {"type": "function_call", "call_id": "real", "name": _tool().name, "arguments": "{}"}}, *ending])
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))),
        usage_observer=tracker)
    events = [event async for event in provider.stream_chat(_request())]
    assert not events[-1].response_complete and events[-1].assistant_blocks is None
    assert tracker.records[0].status == "incomplete"
    assert tracker.records[0].usage.cached_input_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,provider_class", [("openai_provider_config", OpenAIProvider), ("claude_provider_config", ClaudeProvider)])
async def test_native_rejection_reports_manual_fallback_without_replay(request, fixture, provider_class):
    calls = []
    def handler(raw):
        calls.append(raw)
        return httpx.Response(400, json={"error": {"message": "unsupported tools extension"}})
    provider = provider_class(request.getfixturevalue(fixture),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderResponseError, match="关闭.*实验项"):
        _ = [event async for event in provider.stream_chat(_request())]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_responses_outer_generator_close_records_cancelled_request(openai_provider_config):
    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse([{"type": "response.output_text.delta", "delta": "一部分"}])
            await asyncio.Event().wait()
        async def aclose(self):
            pass
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=WaitingStream()))), usage_observer=tracker)
    stream = provider.stream_chat(_request())
    assert (await anext(stream)).kind == "message_start"
    assert (await anext(stream)).text == "一部分"
    await stream.aclose()
    assert tracker.records[0].status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("function", [
    {"type": "function_call", "name": "x", "arguments": "{}"},
    {"type": "function_call", "call_id": "real", "name": "x", "arguments": {}},
])
async def test_responses_rejects_missing_real_tool_identity_or_arguments(openai_provider_config, function):
    provider = OpenAIProvider(openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=_sse([_done([function])])))))
    with pytest.raises(ProviderResponseError, match="真实调用标识"):
        _ = [event async for event in provider.stream_chat(_request())]


def test_responses_invalid_json_preserves_received_call_for_feedback():
    blocks = ResponsesProvider._complete_blocks([{"type": "function_call", "call_id": "real", "name": "x", "arguments": "broken"}])
    assert blocks[0].call_id == "real" and blocks[0].input == {"INVALID_JSON": "broken"}


@pytest.mark.asyncio
async def test_responses_completed_snapshot_cannot_rewrite_streamed_tool_arguments(openai_provider_config):
    body = _sse([
        {"type": "response.output_item.added", "output_index": 0, "item": {
            "type": "function_call", "call_id": "real", "name": "x", "arguments": '{"path":"old"}'}},
        _done([{"type": "function_call", "call_id": "real", "name": "x", "arguments": '{"path":"new"}'}])])
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config, usage_observer=tracker,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))))
    with pytest.raises(ProviderResponseError, match="增量不一致"):
        _ = [event async for event in provider.stream_chat(_request())]
    assert tracker.records[0].status == "failed"


@pytest.mark.asyncio
async def test_responses_missing_usage_never_invents_cache_hit(openai_provider_config):
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config, usage_observer=tracker,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=_sse([_done()])))))
    events = [event async for event in provider.stream_chat(_request())]
    assert events[-1].response_complete
    assert events[-1].usage.cached_input_tokens is None
    assert tracker.snapshot().cache_hit_ratio is None


def test_responses_complete_response_rejects_unfinished_output_item():
    with pytest.raises(ProviderResponseError, match="未完成"):
        ResponsesProvider._complete_blocks([{"type": "function_call", "call_id": "real", "name": "x",
            "arguments": "{}", "status": "incomplete"}])
