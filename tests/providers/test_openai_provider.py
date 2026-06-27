from __future__ import annotations

import json

import httpx
import pytest

from lancher_code.errors import ProviderAuthError
from lancher_code.models import ChatMessage, ChatRequest
from lancher_code.providers.openai import OpenAIProvider


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


@pytest.mark.asyncio
async def test_openai_provider_streams_text_deltas(openai_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = _build_sse_payload(
            [
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {"content": "你"},
                                "finish_reason": None,
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
                                "delta": {"content": "好"},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
                + "\n\n",
                "data: [DONE]\n\n",
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )
    request = ChatRequest(
        model="gpt-test",
        messages=[ChatMessage(role="user", content="你好")],
    )

    events = [event async for event in provider.stream_chat(request)]

    assert [event.kind for event in events] == ["message_start", "text_delta", "text_delta", "message_end"]
    assert "".join(event.text or "" for event in events if event.kind == "text_delta") == "你好"


@pytest.mark.asyncio
async def test_openai_provider_raises_auth_error(openai_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=30.0),
    )
    request = ChatRequest(
        model="gpt-test",
        messages=[ChatMessage(role="user", content="你好")],
    )

    with pytest.raises(ProviderAuthError) as exc_info:
        return [event async for event in provider.stream_chat(request)]

    assert "bad key" in exc_info.value.user_message
