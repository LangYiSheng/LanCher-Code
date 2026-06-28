from __future__ import annotations

import json

import httpx
import pytest

from lancher_code.errors import ProviderAuthError
from lancher_code.models import ChatRequest, ContentBlock, ConversationMessage, ToolDefinition
from lancher_code.providers.openai import OpenAIProvider


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


def _request(*, allow_tool_calls: bool = True) -> ChatRequest:
    return ChatRequest(
        model="gpt-test",
        messages=[ConversationMessage.text_message("user", "你好")],
        tools=[ToolDefinition(name="read_file", description="读取文件", input_schema={"type": "object"})],
        allow_tool_calls=allow_tool_calls,
    )


@pytest.mark.asyncio
async def test_openai_provider_streams_text_deltas_and_usage(openai_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["stream_options"]["include_usage"] is True
        assert payload["tools"][0]["function"]["name"] == "read_file"
        body = _build_sse_payload(
            [
                "data: "
                + json.dumps({"choices": [{"delta": {"content": "你"}, "finish_reason": None}], "usage": None})
                + "\n\n",
                "data: "
                + json.dumps({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}], "usage": None})
                + "\n\n",
                "data: "
                + json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
                + "\n\n",
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request())]

    assert [event.kind for event in events] == ["message_start", "text_delta", "text_delta", "message_end"]
    assert "".join(event.text or "" for event in events if event.kind == "text_delta") == "你好"
    assert events[-1].usage.input_tokens == 3
    assert events[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_openai_provider_parses_tool_call_deltas(openai_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-1",
                                            "function": {"name": "read_file", "arguments": '{"path":"'},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
                + "\n\n",
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": 'demo.txt"}'},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
                + "\n\n",
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request())]
    tool_events = [event for event in events if event.kind == "tool_call_delta"]

    assert len(tool_events) == 2
    assert tool_events[0].tool_call_chunk is not None
    assert tool_events[0].tool_call_chunk.provider_call_id == "call-1"
    assert tool_events[0].tool_call_chunk.name_delta == "read_file"
    assert tool_events[1].tool_call_chunk.arguments_delta == 'demo.txt"}'


@pytest.mark.asyncio
async def test_openai_provider_omits_tools_in_second_pass(openai_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert "tools" not in payload
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_build_sse_payload(["data: [DONE]\n\n"]),
        )

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    events = [event async for event in provider.stream_chat(_request(allow_tool_calls=False))]

    assert [event.kind for event in events] == ["message_start", "message_end"]


@pytest.mark.asyncio
async def test_openai_provider_raises_auth_error(openai_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )

    with pytest.raises(ProviderAuthError) as exc_info:
        return [event async for event in provider.stream_chat(_request())]

    assert "bad key" in exc_info.value.user_message
