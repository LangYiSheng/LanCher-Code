from __future__ import annotations

import json

import httpx
import pytest

from lancher_code.errors import ProviderPromptTooLongError, ProviderResponseError
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage
from lancher_code.providers.models import ThinkingConfig
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.usage.ledger import RunUsageTracker


def _build_sse_payload(chunks: list[str]) -> bytes:
    return "".join(chunks).encode("utf-8")


def _protocol_events(events: list[dict[str, object]]) -> bytes:
    return _build_sse_payload([
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, expected_input", [
    ('{"path":"demo.txt"}', {"path": "demo.txt"}),
    ('{"path":"broken', {"INVALID_JSON": '{"path":"broken'}),
    ('["wrong shape"]', {"INVALID_JSON": '["wrong shape"]'}),
])
async def test_claude_preserves_complete_thinking_exchange_and_original_tool_id(
    claude_provider_config, arguments: str, expected_input: dict[str, object],
) -> None:
    expected = [
        {"type": "thinking", "thinking": "初始思考继续思考", "signature": "sig-start-middle-end"},
        {"type": "redacted_thinking", "data": "opaque-redacted-data"},
        {"type": "text", "text": "先读文件"},
        {"type": "tool_use", "id": "real-call", "name": "read_file", "input": expected_input},
    ]
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            assert payload["messages"][1]["content"] == expected
            assert payload["messages"][2]["content"][0]["tool_use_id"] == "real-call"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_protocol_events([
                {"type": "message_start"},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "完成"}},
                {"type": "message_stop"},
            ]))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_protocol_events([
            {"type": "message_start"},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "初始思考", "signature": "sig-start"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "继续思考"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "-middle"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "-end"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "redacted_thinking", "data": "opaque-redacted-data"}},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": "先读"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "文件"}},
            {"type": "content_block_start", "index": 3,
             "content_block": {"type": "tool_use", "id": "real-call", "name": "read_file", "input": {}}},
            {"type": "content_block_delta", "index": 3,
             "delta": {"type": "input_json_delta", "partial_json": arguments[:7]}},
            {"type": "content_block_delta", "index": 3,
             "delta": {"type": "input_json_delta", "partial_json": arguments[7:]}},
            {"type": "message_stop"},
        ]))

    transport = httpx.MockTransport(handler)
    provider = ClaudeProvider(claude_provider_config,
                              client_factory=lambda: httpx.AsyncClient(transport=transport))
    request = _request(thinking=ThinkingConfig(enabled=True, budget_tokens=512))
    events = [event async for event in provider.stream_chat(request)]
    blocks = events[-1].assistant_blocks
    assert events[-1].response_complete is True
    assert blocks is not None
    assert [block.kind for block in blocks] == ["thinking", "redacted_thinking", "text", "tool_use"]
    assert blocks[0].text == "初始思考继续思考"
    assert blocks[0].signature == "sig-start-middle-end"
    assert blocks[-1].input == expected_input
    assert all("sig-start" not in (event.text or "") and "opaque-redacted" not in (event.text or "")
               for event in events)
    request.messages.extend([
        ConversationMessage(role="assistant", blocks=blocks),
        ConversationMessage(role="tool", blocks=[ContentBlock.tool_result_block(
            call_id="real-call", text="工具反馈", is_error="INVALID_JSON" in expected_input,
        )]),
    ])
    second = [event async for event in provider.stream_chat(request)]
    assert second[-1].assistant_blocks == [ContentBlock.text_block("完成")]
    assert blocks[0].signature == "sig-start-middle-end"


def test_claude_serialization_drops_foreign_protocol_thinking(claude_provider_config) -> None:
    provider = ClaudeProvider(claude_provider_config)
    message = ConversationMessage(role="assistant", blocks=[
        ContentBlock.thinking_block("外来思考", protocol="openai", thinking_field="reasoning"),
        ContentBlock.text_block("正文"),
    ])
    assert provider._serialize_message(message) == {
        "role": "assistant", "content": [{"type": "text", "text": "正文"}],
    }


@pytest.mark.asyncio
async def test_claude_incomplete_stream_does_not_publish_complete_assistant_blocks(claude_provider_config) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_protocol_events([
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "尚未完成"}},
        ]),
    ))
    tracker = RunUsageTracker()
    provider = ClaudeProvider(claude_provider_config,
                              client_factory=lambda: httpx.AsyncClient(transport=transport), usage_observer=tracker)
    stream = provider.stream_chat(_request())
    events = []
    try:
        async for event in stream:
            events.append(event)
            if event.kind == "message_end":
                break
    finally:
        # Runner 发现异常 EOF 后会立即关闭生成器，不能把已确认的异常结束改成用户取消。
        await stream.aclose()
    assert events[-1].assistant_blocks is None
    assert events[-1].response_complete is False
    assert tracker.records[0].status == "incomplete"


@pytest.mark.asyncio
async def test_claude_does_not_fabricate_missing_tool_ids(claude_provider_config) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_protocol_events([
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "name": "read_file", "input": {"path": "demo.txt"}}},
            {"type": "message_stop"},
        ]),
    ))
    tracker = RunUsageTracker()
    provider = ClaudeProvider(claude_provider_config,
                              client_factory=lambda: httpx.AsyncClient(transport=transport), usage_observer=tracker)
    with pytest.raises(ProviderResponseError, match="真实调用标识"):
        _ = [event async for event in provider.stream_chat(_request())]
    assert tracker.records[0].status == "failed"


def _request(*, thinking: ThinkingConfig | None = None, allow_tool_calls: bool = True) -> ChatRequest:
    return ChatRequest(
        model="claude-test",
        system=["稳定系统提示词", "环境提示词"],
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
        assert payload["system"] == "稳定系统提示词\n\n环境提示词"
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
    assert events[-1].usage.cached_input_tokens is None
    assert "input" in events[-1].usage.partial_fields
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
async def test_claude_provider_serializes_multi_block_user_content(claude_provider_config) -> None:
    request = ChatRequest(
        model="claude-test",
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
        assert payload["messages"][0] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>\n用户启用了 Plan Mode\n</system-reminder>"},
                {"type": "text", "text": "计划一下"},
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_build_sse_payload(
                [
                    "event: message_start\n"
                    + "data: "
                    + json.dumps({"type": "message_start"})
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

    events = [event async for event in provider.stream_chat(request)]

    assert [event.kind for event in events] == ["message_start", "message_end"]


@pytest.mark.asyncio
async def test_claude_provider_serializes_parallel_tool_results_in_next_user_message(
    claude_provider_config,
) -> None:
    request = ChatRequest(
        model="claude-test",
        messages=[
            ConversationMessage(
                role="assistant",
                blocks=[
                    ContentBlock.tool_use_block(call_id="call-1", name="glob", input={"pattern": "*"}),
                    ContentBlock.tool_use_block(
                        call_id="call-2", name="grep", input={"pattern": "context"}
                    ),
                ],
            ),
            ConversationMessage(
                role="tool",
                blocks=[
                    ContentBlock.tool_result_block(
                        call_id="call-1", text="找到 9 个文件", is_error=False
                    ),
                    ContentBlock.tool_result_block(
                        call_id="call-2", text="找到 436 条命中", is_error=False
                    ),
                ],
            ),
        ],
    )

    def handler(raw_request: httpx.Request) -> httpx.Response:
        payload = json.loads(raw_request.content.decode("utf-8"))
        assert len(payload["messages"]) == 2
        assert payload["messages"][1] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "找到 9 个文件",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "找到 436 条命中",
                    "is_error": False,
                },
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_build_sse_payload(
                [
                    "event: message_start\n"
                    + "data: "
                    + json.dumps({"type": "message_start"})
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

    events = [event async for event in provider.stream_chat(request)]

    assert [event.kind for event in events] == ["message_start", "message_end"]


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


@pytest.mark.asyncio
async def test_claude_provider_classifies_stream_prompt_too_long(claude_provider_config) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _build_sse_payload(
            [
                "event: error\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "prompt_too_long", "message": "prompt is too long"},
                    }
                )
                + "\n\n"
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    provider = ClaudeProvider(
        claude_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderPromptTooLongError):
        _ = [event async for event in provider.stream_chat(_request())]
