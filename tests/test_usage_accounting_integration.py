from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from lancher_code.errors import ProviderResponseError
from lancher_code.providers.factory import create_provider
from lancher_code.run_usage import RunUsageTracker
from lancher_code.session import SessionController
from lancher_code.sessions import ProjectSessionRepository


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def _initial_usage(protocol: str) -> bytes:
    if protocol == "openai":
        return _sse({"choices": [], "usage": {
            "prompt_tokens": 40, "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 30},
        }})
    return _sse({"type": "message_start", "message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": 25,
        "cache_creation_input_tokens": 5, "output_tokens": 0,
    }}})


def _completed_body(protocol: str) -> bytes:
    initial = _initial_usage(protocol)
    if protocol == "openai":
        return initial + initial + b"data: [DONE]\n\n"
    delta = _sse({"type": "message_delta", "usage": {"output_tokens": 3},
                  "delta": {"stop_reason": "end_turn"}})
    return initial + delta + delta + _sse({"type": "message_stop"})


def _provider(config, tracker, body):
    def handler(_request):
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        return httpx.Response(200, stream=body)

    return create_provider(
        config, usage_observer=tracker,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _bound_request(controller, *, turn_id="turn"):
    controller.create_user_message(f"记录一次真实模型请求：{turn_id}")
    assistant = controller.create_assistant_message()
    request = controller.bind_usage_request(
        controller.build_request([], allow_tool_calls=False), turn_id=turn_id, message_id=assistant.id,
    )
    return request, assistant


async def _collect(controller, provider, request):
    return [event async for event in controller.stream_request(provider, request)]


@pytest.mark.asyncio
@pytest.mark.parametrize("config_fixture", ["openai_provider_config", "claude_provider_config"])
async def test_repeated_provider_frames_are_idempotent_in_session_and_startup(
    request, config_fixture, tmp_path,
):
    config = request.getfixturevalue(config_fixture)
    tracker = RunUsageTracker()
    controller = SessionController(config, cwd=tmp_path, usage_tracker=tracker)
    model_request, assistant = _bound_request(controller)
    provider = _provider(config, tracker, _completed_body(config.protocol))
    try:
        events = await _collect(controller, provider, model_request)
        controller.complete_message(assistant.id)
        summary = controller.usage_summary()
        assert summary == tracker.snapshot()
        assert summary.request_count == summary.completed_request_count == 1
        assert summary.input_tokens == 40
        assert summary.output_tokens == 3
        assert summary.cached_input_tokens == (30 if config.protocol == "openai" else 25)
        assert summary.incomplete_request_count == 0
        assert assistant.usage == summary.usage
        assert events[-1].usage.is_final
        record = tracker.records[0]
        assert record.request_id == model_request.request_id
        assert record.session_id == controller.session_id
        assert record.turn_id == "turn"
        assert record.message_id == assistant.id
    finally:
        controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("config_fixture", ["openai_provider_config", "claude_provider_config"])
async def test_failed_provider_keeps_reported_usage_on_restore_without_importing_old_run(
    request, config_fixture, tmp_path,
):
    config = request.getfixturevalue(config_fixture)
    tracker = RunUsageTracker()
    controller = SessionController(config, cwd=tmp_path, usage_tracker=tracker)
    model_request, assistant = _bound_request(controller)
    error = ({"error": {"message": "模拟帧后故障"}} if config.protocol == "openai"
             else {"type": "error", "error": {"message": "模拟帧后故障"}})
    provider = _provider(config, tracker, _initial_usage(config.protocol) + _sse(error))
    try:
        with pytest.raises(ProviderResponseError):
            await _collect(controller, provider, model_request)
        controller.fail_message(assistant.id, "模拟帧后故障")
        before = controller.usage_summary()
        assert before == tracker.snapshot()
        assert before.input_tokens == 40
        assert before.output_tokens == (3 if config.protocol == "openai" else 0)
        assert before.incomplete_request_count == 1
        assert tracker.records[0].status == "failed"
        assert not before.usage.is_final
        session_id = controller.session_id
    finally:
        controller.close()

    new_tracker = RunUsageTracker()
    restored = SessionController(config, cwd=tmp_path, usage_tracker=new_tracker)
    try:
        restored.resume_session(session_id)
        assert restored.usage_summary() == before
        assert restored.state.messages[-1].usage == before.usage
        assert new_tracker.snapshot().request_count == 0
        assert new_tracker.snapshot().total_tokens == 0
    finally:
        restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("config_fixture", ["openai_provider_config", "claude_provider_config"])
async def test_cancelled_provider_keeps_usage_in_both_ledgers_and_after_restore(
    request, config_fixture, tmp_path,
):
    config = request.getfixturevalue(config_fixture)
    reached_wait = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _initial_usage(config.protocol)
            reached_wait.set()
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    tracker = RunUsageTracker()
    controller = SessionController(config, cwd=tmp_path, usage_tracker=tracker)
    model_request, assistant = _bound_request(controller)
    provider = _provider(config, tracker, WaitingStream())
    task = asyncio.create_task(_collect(controller, provider, model_request))
    try:
        await asyncio.wait_for(reached_wait.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        controller.cancel_message(assistant.id)
        before = controller.usage_summary()
        assert before == tracker.snapshot()
        assert before.input_tokens == 40
        assert before.incomplete_request_count == 1
        assert before.cache_hit_ratio is None
        assert tracker.records[0].status == "cancelled"
        session_id = controller.session_id
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        controller.close()

    new_tracker = RunUsageTracker()
    restored = SessionController(config, cwd=tmp_path, usage_tracker=new_tracker)
    try:
        restored.resume_session(session_id)
        assert restored.usage_summary() == before
        assert restored.state.messages[-1].status == "cancelled"
        assert new_tracker.snapshot().request_count == 0
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_shared_provider_concurrent_requests_keep_session_and_message_ownership(
    openai_provider_config, tmp_path,
):
    tracker = RunUsageTracker()
    first = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    second = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    first_request, first_assistant = _bound_request(first, turn_id="first-turn")
    second_request, second_assistant = _bound_request(second, turn_id="second-turn")
    barrier = asyncio.Event()
    started = 0

    class ConcurrentStream(httpx.AsyncByteStream):
        def __init__(self, count):
            self.count = count

        async def __aiter__(self):
            nonlocal started
            started += 1
            if started == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=5)
            yield _sse({"choices": [], "usage": {
                "prompt_tokens": self.count, "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 0},
            }})
            await asyncio.sleep(0)
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            pass

    def handler(raw_request):
        body = json.loads(raw_request.content)
        assert body["model"] == openai_provider_config.model
        users = json.dumps([message for message in body["messages"] if message["role"] == "user"])
        assert "first-turn" in users or "second-turn" in users
        count = 10 if "first-turn" in users else 30
        return httpx.Response(200, stream=ConcurrentStream(count))

    provider = create_provider(
        openai_provider_config, usage_observer=tracker,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await asyncio.gather(_collect(first, provider, first_request), _collect(second, provider, second_request))
        first.complete_message(first_assistant.id)
        second.complete_message(second_assistant.id)
        assert first.usage_summary().input_tokens == 10
        assert second.usage_summary().input_tokens == 30
        assert first_assistant.usage.input_tokens == 10
        assert second_assistant.usage.input_tokens == 30
        assert first.usage_summary(first_assistant.id).input_tokens == 10
        assert second.usage_summary(second_assistant.id).input_tokens == 30
        assert tracker.snapshot().request_count == 2
        assert tracker.snapshot().input_tokens == 40
        ownership = {record.request_id: (record.session_id, record.turn_id, record.message_id)
                     for record in tracker.records}
        assert ownership == {
            first_request.request_id: (first.session_id, "first-turn", first_assistant.id),
            second_request.request_id: (second.session_id, "second-turn", second_assistant.id),
        }
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_usage_event_replay_matches_checkpoint_when_checkpoint_is_missing(
    openai_provider_config, tmp_path,
):
    tracker = RunUsageTracker()
    controller = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    model_request, assistant = _bound_request(controller)
    provider = _provider(openai_provider_config, tracker, _completed_body("openai"))
    try:
        await _collect(controller, provider, model_request)
        controller.complete_message(assistant.id)
        expected = controller.usage_summary()
        expected_records = copy.deepcopy(controller.state.request_usage)
        session_id, paths = controller.session_id, controller.paths
    finally:
        controller.close()

    events = ProjectSessionRepository(tmp_path).read(session_id)
    updates = [event for event in events if event["type"] == "usage.request_updated"]
    assert len(updates) >= 3
    assert {event["data"]["request_id"] for event in updates} == {model_request.request_id}

    from_checkpoint = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        from_checkpoint.resume_session(session_id)
        assert from_checkpoint.usage_summary() == expected
        assert from_checkpoint.state.request_usage == expected_records
    finally:
        from_checkpoint.close()

    paths.checkpoint.unlink()
    from_events = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        from_events.resume_session(session_id)
        assert from_events.usage_summary() == expected
        assert from_events.state.request_usage == expected_records
        assert from_events.state.messages[-1].usage == expected.usage
        assert from_events._usage_tracker.snapshot().request_count == 0
    finally:
        from_events.close()


@pytest.mark.asyncio
async def test_crash_after_only_usage_events_rebuilds_message_usage_from_request_ledger(
    openai_provider_config, tmp_path, monkeypatch,
):
    tracker = RunUsageTracker()
    controller = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    model_request, assistant = _bound_request(controller)
    provider = _provider(openai_provider_config, tracker, _completed_body("openai"))
    # 消息创建已经落盘；模拟后续全量状态尚未来得及保存就中断进程。
    monkeypatch.setattr(controller, "flush", lambda **_kwargs: None)
    try:
        await _collect(controller, provider, model_request)
        expected = controller.usage_summary()
        session_id, paths = controller.session_id, controller.paths
    finally:
        controller._sessions.close()
    paths.checkpoint.unlink(missing_ok=True)
    events = ProjectSessionRepository(tmp_path).read(session_id)
    assert any(event["type"] == "usage.request_updated" and event["data"]["status"] == "completed"
               for event in events)
    assert not any(event["type"] == "message.updated" and "usage" in event["data"].get("fields", {})
                   for event in events)

    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        assert restored.usage_summary() == expected
        message = restored.state.messages[-1]
        assert message.id == assistant.id
        assert message.status == "cancelled"
        assert message.usage == expected.usage
        assert message.usage.input_tokens == 40
        assert message.usage.output_tokens == 3
        assert restored._usage_tracker.snapshot().request_count == 0
    finally:
        restored.close()
