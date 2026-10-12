from __future__ import annotations

from contextlib import aclosing

from provider_helpers import complete_test_response

import pytest

from lancher_code.contracts.messages import ChatRequest, StreamEvent
from lancher_code.usage.models import MessageUsage
from lancher_code.usage.tracking import tracked_stream
from lancher_code.usage.ledger import RequestUsageRecord


@pytest.mark.asyncio
async def test_misrouted_callback_cannot_create_a_foreign_usage_record():
    records = []
    request = ChatRequest(model="model", session_id="session", usage_callback=records.append)

    class MisroutedProvider:
        @complete_test_response
        async def stream_chat(self, attempt):
            record = RequestUsageRecord(
                request_id="other-request", run_id="run", protocol="openai", model="model",
                session_id="session", usage=MessageUsage(input_tokens=999),
            )
            attempt.usage_callback(record.to_dict())
            yield StreamEvent(response_complete=True, kind="message_end")

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
        @complete_test_response
        async def stream_chat(self, _attempt):
            raise AssertionError("记录开始失败时不应调用模型")
            yield

    with pytest.raises(OSError, match="保存失败"):
        _ = [event async for event in tracked_stream(UnusedProvider(), request,
                                                     protocol="openai", run_id="run")]
    assert request.usage_callback is fail


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,complete,expected", [
    ("length", True, "incomplete"), ("max_tokens", True, "incomplete"),
    (None, True, "incomplete"), ("stop", True, "completed"),
    ("length", False, "incomplete"),
    ("content_filter", True, "incomplete"), ("refusal", True, "incomplete"),
    ("aborted", True, "incomplete"), ("insufficient_system_resource", True, "incomplete"),
    ("pause_turn", True, "incomplete"),
])
async def test_custom_stream_records_generation_completion_and_reason(reason, complete, expected):
    records = []
    request = ChatRequest(model="model", max_output_tokens=5, usage_callback=records.append)

    class CustomProvider:
        async def stream_chat(self, _attempt):
            yield StreamEvent(kind="text_delta", text="已有回答")
            yield StreamEvent(kind="message_end", usage=MessageUsage(output_tokens=5), stop_reason=reason,
                              response_complete=complete, terminal_received=complete,
                              assistant_blocks=[] if complete else None)

    _ = [event async for event in tracked_stream(CustomProvider(), request, protocol="openai", run_id="run")]
    final = RequestUsageRecord.from_dict(records[-1])
    assert final.status == expected
    assert final.stop_reason == reason
    assert final.max_output_tokens == 5
    assert final.usage.output_tokens == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "aborted"])
async def test_closing_custom_stream_at_incomplete_terminal_keeps_incomplete_status(reason):
    records = []
    request = ChatRequest(model="model", max_output_tokens=5, usage_callback=records.append)

    class CustomProvider:
        async def stream_chat(self, _attempt):
            yield StreamEvent(kind="message_end", usage=MessageUsage(output_tokens=5), stop_reason=reason,
                              response_complete=True, terminal_received=True, assistant_blocks=[])

    async with aclosing(tracked_stream(CustomProvider(), request, protocol="openai", run_id="run")) as stream:
        async for event in stream:
            if event.kind == "message_end":
                break
    assert records[-1]["status"] == "incomplete"
    assert records[-1]["stop_reason"] == reason
