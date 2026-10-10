from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from lancher_code.errors import ProviderAuthError, ProviderPromptTooLongError, ProviderResponseError
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.usage.ledger import RunUsageTracker


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_field", ["reasoning_content", "reasoning"])
@pytest.mark.parametrize("arguments, expected_input", [
    ('{"path":"demo.txt"}', {"path": "demo.txt"}),
    ('{"path":"broken', {"INVALID_JSON": '{"path":"broken'}),
])
async def test_openai_reasoning_and_tool_exchange_round_trip_uses_original_field(
    openai_provider_config, reasoning_field: str, arguments: str, expected_input: dict[str, object],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 2:
            assistant = payload["messages"][-2]
            assert assistant[reasoning_field] == "先分析再读取"
            assert ({"reasoning", "reasoning_content"} - {reasoning_field}).isdisjoint(assistant)
            assert assistant["content"] == "读取文件"
            tool = assistant["tool_calls"][0]
            assert tool["id"] == "original-call"
            assert tool["function"]["name"] == "read_file"
            assert json.loads(tool["function"]["arguments"]) == expected_input
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_build_sse_payload(["data: [DONE]\n\n"]))
        deltas = [
            {reasoning_field: "先分析"},
            {reasoning_field: "再读取"},
            {"content": "读取文件"},
            {"tool_calls": [{"index": 0, "id": "original-call",
                             "function": {"name": "read_file", "arguments": arguments[:7]}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": arguments[7:]}}]},
        ]
        chunks = [f"data: {json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False)}\n\n"
                  for delta in deltas]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_build_sse_payload([*chunks, "data: [DONE]\n\n"]))

    transport = httpx.MockTransport(handler)
    provider = OpenAIProvider(openai_provider_config,
                              client_factory=lambda: httpx.AsyncClient(transport=transport))
    request = _request()
    events = [event async for event in provider.stream_chat(request)]
    blocks = events[-1].assistant_blocks
    assert events[-1].response_complete is True
    assert blocks is not None
    assert [block.kind for block in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[0].thinking_protocol == "openai"
    assert blocks[0].thinking_field == reasoning_field
    request.messages.extend([
        ConversationMessage(role="assistant", blocks=blocks),
        ConversationMessage(role="tool", blocks=[ContentBlock.tool_result_block(
            call_id="original-call", text="反馈", is_error="INVALID_JSON" in expected_input,
        )]),
    ])
    second = [event async for event in provider.stream_chat(request)]
    assert second[-1].assistant_blocks == []
    assert blocks[0].text == "先分析再读取"


def test_openai_does_not_manufacture_or_accept_foreign_reasoning_fields(openai_provider_config) -> None:
    provider = OpenAIProvider(openai_provider_config)
    message = ConversationMessage(role="assistant", blocks=[
        ContentBlock.thinking_block("Claude 思考", signature="opaque-signature"),
        ContentBlock.redacted_thinking_block("opaque-data"),
        ContentBlock.text_block("正文"),
    ])
    assert provider._serialize_message(message) == {"role": "assistant", "content": "正文"}
    assert provider._serialize_message(ConversationMessage.text_message("assistant", "普通回答")) == {
        "role": "assistant", "content": "普通回答",
    }


@pytest.mark.asyncio
async def test_openai_incomplete_stream_does_not_publish_complete_assistant_blocks(openai_provider_config) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"reasoning_content":"not finished"}}]}\n\n',
    ))
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config,
                              client_factory=lambda: httpx.AsyncClient(transport=transport), usage_observer=tracker)
    stream = provider.stream_chat(_request())
    events = []
    try:
        async for event in stream:
            events.append(event)
            if event.kind == "message_end":
                break
    finally:
        await stream.aclose()
    assert events[-1].assistant_blocks is None
    assert events[-1].response_complete is False
    assert tracker.records[0].status == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("provide_id", [False, True])
async def test_openai_requires_actual_tool_id_but_accepts_it_in_later_delta(
    openai_provider_config, provide_id: bool,
) -> None:
    deltas = [
        {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":"'}}]},
        {"tool_calls": [{"index": 0, **({"id": "later-real-id"} if provide_id else {}),
                         "function": {"name": "read_file", "arguments": 'demo.txt"}'}}]},
    ]
    chunks = [f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n" for delta in deltas]
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_build_sse_payload([*chunks, "data: [DONE]\n\n"]),
    ))
    tracker = RunUsageTracker()
    provider = OpenAIProvider(openai_provider_config,
                              client_factory=lambda: httpx.AsyncClient(transport=transport), usage_observer=tracker)
    if not provide_id:
        with pytest.raises(ProviderResponseError, match="真实调用标识"):
            _ = [event async for event in provider.stream_chat(_request())]
        assert tracker.records[0].status == "failed"
        return
    events = [event async for event in provider.stream_chat(_request())]
    assert events[-1].assistant_blocks == [ContentBlock.tool_use_block(
        call_id="later-real-id", name="read_file", input={"path": "demo.txt"},
    )]


def _request(*, allow_tool_calls: bool = True) -> ChatRequest:
    return ChatRequest(
        model="gpt-test",
        system=["稳定系统提示词", "环境提示词"],
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
        assert payload["messages"][0] == {"role": "system", "content": "稳定系统提示词"}
        assert payload["messages"][1] == {"role": "system", "content": "环境提示词"}
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
    assert events[-1].usage.cached_input_tokens is None
    assert events[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_openai_provider_parses_cached_input_tokens(openai_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "data: "
                + json.dumps(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 9,
                            "completion_tokens": 4,
                            "prompt_tokens_details": {"cached_tokens": 6},
                        },
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

    assert events[-1].usage.input_tokens == 9
    assert events[-1].usage.cached_input_tokens == 6
    assert events[-1].usage.output_tokens == 4


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
async def test_openai_provider_serializes_multi_block_user_content(openai_provider_config) -> None:
    request = ChatRequest(
        model="gpt-test",
        system=["稳定系统提示词"],
        messages=[
            ConversationMessage(
                role="user",
                blocks=[
                    ContentBlock.text_block("<system-reminder>\n用户启用了 Plan Mode\n</system-reminder>"),
                    ContentBlock.text_block("计划一下"),
                ],
            )
        ],
    )

    def handler(raw_request: httpx.Request) -> httpx.Response:
        payload = json.loads(raw_request.content.decode("utf-8"))
        assert payload["messages"][1] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>\n用户启用了 Plan Mode\n</system-reminder>"},
                {"type": "text", "text": "计划一下"},
            ],
        }
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

    events = [event async for event in provider.stream_chat(request)]

    assert [event.kind for event in events] == ["message_start", "message_end"]


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


@pytest.mark.asyncio
async def test_openai_provider_classifies_only_known_prompt_too_long_error(openai_provider_config) -> None:
    def too_long_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "context_length_exceeded", "message": "too many tokens"}},
        )

    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(too_long_handler)),
    )
    with pytest.raises(ProviderPromptTooLongError):
        _ = [event async for event in provider.stream_chat(_request())]

    def ordinary_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "invalid_request", "message": "bad field"}})

    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(ordinary_handler)),
    )
    with pytest.raises(ProviderResponseError):
        _ = [event async for event in provider.stream_chat(_request())]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["o3", "ordinary-model-without-name-inference"])
@pytest.mark.parametrize("base_url, limit_field", [
    ("https://api.openai.com/v1", "max_completion_tokens"),
    ("https://API.OPENAI.COM/v1", "max_completion_tokens"),
    ("https://api.openai.com:443/v1", "max_completion_tokens"),
    ("https://api.deepseek.com", "max_tokens"),
    ("https://api.deepseek.com/v1", "max_tokens"),
    ("https://unknown.example/v1", "max_tokens"),
    ("https://api.openai.com.example/v1", "max_tokens"),
    ("https://unknown.example/api.openai.com/v1", "max_tokens"),
])
async def test_openai_output_limit_uses_exact_endpoint_host_without_model_name_inference(
    openai_provider_config, base_url, limit_field, model,
) -> None:
    config = replace(openai_provider_config, base_url=base_url)
    model_request = _request()
    model_request.model = model
    model_request.max_output_tokens = 1536
    calls = []

    def handler(raw_request):
        payload = json.loads(raw_request.content)
        calls.append(payload)
        assert payload["model"] == model
        assert payload[limit_field] == 1536
        assert {field for field in ("max_tokens", "max_completion_tokens") if field in payload} == {limit_field}
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    provider = OpenAIProvider(config, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _ = [event async for event in provider.stream_chat(model_request)]
    assert model_request.max_output_tokens == 1536
    assert len(calls) == 1
