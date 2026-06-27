from __future__ import annotations

import json

import httpx
import pytest

from lancher_code.errors import ProviderAuthError
from lancher_code.models import ApiMessage, ChatRequest
from lancher_code.providers.openai import OpenAIProvider


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


@pytest.mark.asyncio
async def test_openai_provider_streams_text_deltas_and_usage(openai_provider_config) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["stream_options"]["include_usage"] is True
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
                        ],
                        "usage": None,
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
                        ],
                        "usage": None,
                    }
                )
                + "\n\n",
                "data: "
                + json.dumps(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
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
        messages=[ApiMessage(role="user", content="你好")],
    )

    events = [event async for event in provider.stream_chat(request)]

    assert [event.kind for event in events] == ["message_start", "text_delta", "text_delta", "message_end"]
    assert "".join(event.text or "" for event in events if event.kind == "text_delta") == "你好"
    assert events[-1].usage.input_tokens == 3
    assert events[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_openai_provider_returns_zero_usage_when_missing(openai_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {"content": "你好"},
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
        messages=[ApiMessage(role="user", content="你好")],
    )

    events = [event async for event in provider.stream_chat(request)]

    assert events[-1].usage.input_tokens == 0
    assert events[-1].usage.output_tokens == 0


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
        messages=[ApiMessage(role="user", content="你好")],
    )

    with pytest.raises(ProviderAuthError) as exc_info:
        return [event async for event in provider.stream_chat(request)]

    assert "bad key" in exc_info.value.user_message
