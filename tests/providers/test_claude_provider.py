from __future__ import annotations

import json

import httpx
import pytest

from lancher_code.errors import ProviderResponseError
from lancher_code.models import ChatMessage, ChatRequest, ThinkingConfig
from lancher_code.providers.claude import ClaudeProvider


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


@pytest.mark.asyncio
async def test_claude_provider_streams_text_and_thinking(claude_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["thinking"]["type"] == "enabled"
        body = _build_sse_payload(
            [
                "event: message_start\n"
                + "data: "
                + json.dumps({"type": "message_start"})
                + "\n\n",
                "event: content_block_delta\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "thinking_delta", "thinking": "先想想"},
                    }
                )
                + "\n\n",
                "event: content_block_delta\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "你好"},
                    }
                )
                + "\n\n",
                "event: message_stop\n"
                + "data: "
                + json.dumps({"type": "message_stop"})
                + "\n\n",
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )
    request = ChatRequest(
        model="claude-test",
        messages=[ChatMessage(role="user", content="你好")],
        thinking=ThinkingConfig(enabled=True, budget_tokens=512),
    )

    events = [event async for event in provider.stream_chat(request)]

    assert [event.kind for event in events] == [
        "message_start",
        "thinking_delta",
        "text_delta",
        "message_end",
    ]
    assert "".join(event.text or "" for event in events if event.kind == "text_delta") == "你好"


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
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )
    request = ChatRequest(
        model="claude-test",
        messages=[ChatMessage(role="user", content="你好")],
    )

    with pytest.raises(ProviderResponseError) as exc_info:
        return [event async for event in provider.stream_chat(request)]

    assert "bad request" in exc_info.value.user_message
