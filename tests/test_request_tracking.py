from __future__ import annotations

import pytest

from lancher_code.models import ChatRequest, MessageUsage, StreamEvent
from lancher_code.request_tracking import tracked_stream
from lancher_code.run_usage import RequestUsageRecord


@pytest.mark.asyncio
async def test_misrouted_callback_cannot_create_a_foreign_usage_record():
    records = []
    request = ChatRequest(model="model", session_id="session", usage_callback=records.append)

    class MisroutedProvider:
        async def stream_chat(self, attempt):
            record = RequestUsageRecord(
                request_id="other-request", run_id="run", protocol="openai", model="model",
                session_id="session", usage=MessageUsage(input_tokens=999),
            )
            attempt.usage_callback(record.to_dict())
            yield StreamEvent(kind="message_end")

    with pytest.raises(ValueError, match="身份或归属"):
        _ = [event async for event in tracked_stream(MisroutedProvider(), request,
                                                     protocol="openai", run_id="run")]

    assert {record['request_id'] for record in records} == {request.request_id}
    assert records[-1]['status'] == 'failed'
    assert records[-1]['usage']['input_tokens'] is None


@pytest.mark.asyncio
async def test_failed_initial_callback_does_not_modify_the_callers_callback():
    def fail(_record):
        raise OSError("保存失败")

    request = ChatRequest(model="model", usage_callback=fail)

    class UnusedProvider:
        async def stream_chat(self, _attempt):
            raise AssertionError("记录开始失败时不应调用模型")
            yield

    with pytest.raises(OSError, match="保存失败"):
        _ = [event async for event in tracked_stream(UnusedProvider(), request,
                                                     protocol="openai", run_id="run")]
    assert request.usage_callback is fail
