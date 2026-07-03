from __future__ import annotations

import json

import httpx
import pytest

from lancher_code.errors import ProviderResponseError
from lancher_code.models import ChatRequest, ConversationMessage, ThinkingConfig, ToolDefinition
from lancher_code.providers.claude import ClaudeProvider


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


def _request(*, thinking: ThinkingConfig | None = None, allow_tool_calls: bool = True) -> ChatRequest:
    return ChatRequest(
        model="claude-test",
        messages=[ConversationMessage.text_message("user", "你好")],
        tools=[ToolDefinition(name="read_file", description="读取文件", input_schema={"type": "object"})],
        allow_tool_calls=allow_tool_calls,
        thinking=thinking,
    )


@pytest.mark.asyncio
async def test_claude_provider_streams_text_thinking_and_usage(claude_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["thinking"]["type"] == "enabled"
        assert payload["tools"][0]["name"] == "read_file"
        body = _build_sse_payload(
            [
                "event: message_start\n"
                + "data: "
                + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 11, "output_tokens": 0}}})
                + "\n\n",
                "event: content_block_delta\n"
                + "data: "
                + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先想想"}})
                + "\n\n",
                "event: content_block_delta\n"
                + "data: "
                + json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "你好"}})
                + "\n\n",
                "event: message_delta\n"
                + "data: "
                + json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}})
                + "\n\n",
                "event: message_stop\n"
                + "data: "
                + json.dumps({"type": "message_stop"})
                + "\n\n",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request(thinking=ThinkingConfig(enabled=True, budget_tokens=512)))]

    assert [event.kind for event in events] == ["message_start", "thinking_delta", "text_delta", "message_end"]
    assert "".join(event.text or "" for event in events if event.kind == "text_delta") == "你好"
    assert events[-1].usage.input_tokens == 11
    assert events[-1].usage.cached_input_tokens == 0
    assert events[-1].usage.output_tokens == 5


@pytest.mark.asyncio
async def test_claude_provider_merges_cached_input_tokens(claude_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "event: message_start\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "message_start",
                        "message": {
                            "usage": {
                                "input_tokens": 11,
                                "cache_creation_input_tokens": 2,
                                "cache_read_input_tokens": 7,
                                "output_tokens": 0,
                            }
                        },
                    }
                )
                + "\n\n",
                "event: message_delta\n"
                + "data: "
                + json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}})
                + "\n\n",
                "event: message_stop\n"
                + "data: "
                + json.dumps({"type": "message_stop"})
                + "\n\n",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request())]

    assert events[-1].usage.input_tokens == 20
    assert events[-1].usage.cached_input_tokens == 7
    assert events[-1].usage.output_tokens == 5


@pytest.mark.asyncio
async def test_claude_provider_parses_tool_call_deltas(claude_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "event: message_start\n"
                + "data: "
                + json.dumps({"type": "message_start"})
                + "\n\n",
                "event: content_block_start\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"},
                    }
                )
                + "\n\n",
                "event: content_block_delta\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": '{"path":"demo.txt"}'},
                    }
                )
                + "\n\n",
                "event: message_stop\n"
                + "data: "
                + json.dumps({"type": "message_stop"})
                + "\n\n",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request())]
    tool_events = [event for event in events if event.kind == "tool_call_delta"]

    assert len(tool_events) == 2
    assert tool_events[0].tool_call_chunk is not None
    assert tool_events[0].tool_call_chunk.provider_call_id == "toolu_1"
    assert tool_events[0].tool_call_chunk.name_delta == "read_file"
    assert tool_events[1].tool_call_chunk.arguments_delta == '{"path":"demo.txt"}'


@pytest.mark.asyncio
async def test_claude_provider_disables_thinking_when_config_disabled(claude_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["thinking"]["type"] == "disabled"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_build_sse_payload(
                [
                    "event: message_start\n"
                    + "data: "
                    + json.dumps({"type": "message_start"})
                    + "\n\n",
                    "event: content_block_delta\n"
                    + "data: "
                    + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "正常正文"}})
                    + "\n\n",
                    "event: message_stop\n"
                    + "data: "
                    + json.dumps({"type": "message_stop"})
                    + "\n\n",
                ]
            ),
        )

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request(thinking=ThinkingConfig(enabled=False, budget_tokens=512)))]

    assert [event.kind for event in events] == ["message_start", "text_delta", "message_end"]
    assert events[1].text == "正常正文"


@pytest.mark.asyncio
async def test_claude_provider_raises_response_error(claude_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "event: error\n"
                + "data: "
                + json.dumps({"type": "error", "error": {"message": "bad request"}})
                + "\n\n"
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    with pytest.raises(ProviderResponseError) as exc_info:
        return [event async for event in provider.stream_chat(_request())]

    assert "bad request" in exc_info.value.user_message
