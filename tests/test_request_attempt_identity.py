"""复用请求内容时，实际发送仍必须是独立尝试。"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from lancher_code.providers.openai import OpenAIProvider
from lancher_code.run_usage import RunUsageTracker
from lancher_code.session import SessionController


RESPONSE = (
    'data: {"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}], '
    '"usage": {"prompt_tokens": 100, "completion_tokens": 10, '
    '"prompt_tokens_details": {"cached_tokens": 0}}}\n\n'
    'data: [DONE]\n\n'
)


@pytest.fixture
def tracked_context(openai_provider_config, tmp_path):
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    session.create_user_message("检查每次实际请求的身份")
    assistant = session.create_assistant_message()

    async def respond(_request):
        # 让两个并发流均有机会开始，实际不访问网络。
        await asyncio.sleep(0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=RESPONSE.encode("utf-8"))

    provider = OpenAIProvider(
        openai_provider_config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        usage_observer=tracker,
    )
    request = session.bind_usage_request(
        session.build_request([], allow_tool_calls=True), turn_id="identity-check", message_id=assistant.id,
    )
    try:
        yield session, tracker, provider, request, assistant.id
    finally:
        session.close()


async def consume(session, provider, request):
    return [event async for event in session.stream_request(provider, request)]


def assert_two_attempts(session, tracker, assistant_id):
    # wrapper 与原生 Provider 共用每次尝试的 ID，不能每次多出一条空请求。
    assert len(tracker.records) == 2
    assert len({record.request_id for record in tracker.records}) == 2
    assert all(record.status == "completed" for record in tracker.records)
    assert all(record.session_id == session.session_id for record in tracker.records)
    assert all(record.turn_id == "identity-check" for record in tracker.records)
    assert all(record.message_id == assistant_id for record in tracker.records)
    assert tracker.snapshot().input_tokens == 200
    assert tracker.snapshot().output_tokens == 20
    assert tracker.snapshot().incomplete_request_count == 0
    assert session.total_usage().input_tokens == 200
    assert session.usage_summary(assistant_id).output_tokens == 20


@pytest.mark.asyncio
async def test_reusing_request_content_creates_a_new_actual_attempt(tracked_context):
    session, tracker, provider, request, assistant_id = tracked_context

    await consume(session, provider, request)
    await consume(session, provider, request)

    assert_two_attempts(session, tracker, assistant_id)


@pytest.mark.asyncio
async def test_concurrent_reuse_keeps_attempt_ids_and_callbacks_independent(tracked_context):
    session, tracker, provider, request, assistant_id = tracked_context

    responses = await asyncio.gather(
        consume(session, provider, request), consume(session, provider, request),
    )

    assert all(sum(event.kind == "message_end" for event in events) == 1 for events in responses)
    assert_two_attempts(session, tracker, assistant_id)
