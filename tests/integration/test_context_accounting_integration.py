from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from provider_helpers import complete_test_response

from lancher_code.agent.runner import TurnRunner
from lancher_code.context.budget import context_budget
from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.messages import StreamEvent
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.errors import ContextCompactionError
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.factory import create_provider
from lancher_code.providers.models import ThinkingConfig
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.usage.ledger import RunUsageTracker
from lancher_code.usage.models import MessageUsage


class SummaryProvider:
    def __init__(self, text, *, stop_reason=None):
        self.text = text
        self.stop_reason = stop_reason
        self.requests = []

    @complete_test_response
    async def stream_chat(self, request):
        self.requests.append(request)
        yield StreamEvent(kind="text_delta", text=self.text)
        yield StreamEvent(kind="message_end", stop_reason=self.stop_reason,
                          usage=MessageUsage(input_tokens=100, output_tokens=50, cached_input_tokens=0), response_complete=True)


def _summary():
    from lancher_code.context.summary import SUMMARY_HEADINGS
    return "<summary>" + "\n".join(f"## {heading}\n已整理。" for heading in SUMMARY_HEADINGS) + "</summary>"


def _long_history(session):
    session.create_user_message("旧任务材料" + "x" * 50_000)
    message = session.create_assistant_message()
    session.append_message_content(message.id, "旧材料已记录。")
    session.complete_message(message.id)
    session.create_user_message("继续处理，保留我的这一条原话。")


def _sse(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def _openai_body(text, *, input_tokens, output_tokens):
    usage = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
             "prompt_tokens_details": {"cached_tokens": 0}}
    # 重复的累计帧不能把一次格式重试记成更多消耗。
    return (_sse({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]})
            + _sse({"choices": [], "usage": usage}) * 2 + b"data: [DONE]\n\n")


def _mock_summary_provider(config, tracker, responses):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = responses[len(requests) - 1]
        if isinstance(response, bytes):
            return httpx.Response(200, content=response)
        return httpx.Response(200, stream=response)

    provider = create_provider(config, usage_observer=tracker,
                               client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return provider, requests


def _runner(provider, session, root):
    registry = ToolRegistry()
    return TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=root))


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["bare", "fence", "outside_text"])
async def test_local_summary_format_normalization_needs_only_one_accounted_request(
    openai_provider_config, shape,
):
    text = _summary()
    if shape == "bare":
        text = text[len("<summary>"):-len("</summary>")]
    elif shape == "fence":
        text = "```markdown\n" + text + "\n```"
    else:
        text = "下面是整理后的摘要：\n" + text + "\n以上是本轮摘要。"
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, usage_tracker=tracker)
    provider = SummaryProvider(text)
    _long_history(session)
    try:
        result = await session.compact_context(provider=provider, visible_tools=[])
        assert result.after_tokens < result.before_tokens
        assert len(provider.requests) == tracker.snapshot().request_count == 1
        assert session.usage_summary().total_tokens == tracker.snapshot().total_tokens == 150
        assert next(iter(session.state.compaction_activities.values())).status == "completed"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_real_provider_format_retry_is_two_requests_in_one_activity_and_survives_restore(
    openai_provider_config, tmp_path,
):
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    _long_history(session)
    provider, requests = _mock_summary_provider(openai_provider_config, tracker, [
        _openai_body("缺少摘要章节，必须重新总结。", input_tokens=100, output_tokens=50),
        _openai_body(_summary(), input_tokens=200, output_tokens=60),
    ])
    runner = _runner(provider, session, tmp_path)
    activity = session.begin_compaction("manual")
    events = []

    async def observe(event):
        events.append(event)

    try:
        result = await runner.compact_context(activity_id=activity.id, on_activity=observe)
        assert result.after_tokens < result.before_tokens
        assert [event.compaction.status for event in events] == ["running", "completed"]
        assert {event.compaction.id for event in events} == {activity.id}
        assert len(session.state.compaction_activities) == 1
        assert len(requests) == 2
        assert requests[0]["messages"][:-1] == requests[1]["messages"][:-1]
        assert requests[0]["messages"][-1] != requests[1]["messages"][-1]
        assert requests[0]["max_tokens"] == requests[1]["max_tokens"]
        assert all(request.get("tools", []) == [] for request in requests)
        assert len({record.request_id for record in tracker.records}) == 2
        assert [record.status for record in tracker.records] == ["completed", "completed"]
        assert all(record.purpose == "compaction" and record.session_id == session.session_id
                   and record.run_id == tracker.run_id for record in tracker.records)
        assert session.usage_summary() == tracker.snapshot()
        assert tracker.snapshot().input_tokens == 300
        assert tracker.snapshot().output_tokens == 110
        assert tracker.snapshot().total_tokens == 410
        expected = session.usage_summary()
        expected_records = deepcopy(session.state.request_usage)
        session_id = session.session_id
    finally:
        session.close()

    new_tracker = RunUsageTracker()
    restored = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=new_tracker)
    try:
        restored.resume_session(session_id)
        assert restored.usage_summary() == expected
        assert restored.state.request_usage == expected_records
        assert restored.get_compaction(activity.id).status == "completed"
        assert new_tracker.snapshot().request_count == 0
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_cancel_format_retry_keeps_both_reported_usages_and_cancels_same_activity(
    openai_provider_config, tmp_path,
):
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    _long_history(session)
    original = deepcopy(session.transcript)
    reached_wait = asyncio.Event()

    class RepairStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse({"choices": [], "usage": {
                "prompt_tokens": 200, "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 0},
            }})
            reached_wait.set()
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    provider, requests = _mock_summary_provider(openai_provider_config, tracker, [
        _openai_body("缺少摘要章节，必须重新总结。", input_tokens=100, output_tokens=50), RepairStream(),
    ])
    runner = _runner(provider, session, tmp_path)
    activity = session.begin_compaction("manual")
    events = []

    async def observe(event):
        events.append(event)

    task = asyncio.create_task(runner.compact_context(activity_id=activity.id, on_activity=observe))
    try:
        await asyncio.wait_for(reached_wait.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(requests) == 2
        assert [event.compaction.status for event in events] == ["running", "cancelled"]
        assert {event.compaction.id for event in events} == {activity.id}
        assert len(session.state.compaction_activities) == 1
        assert session.transcript == original
        assert len({record.request_id for record in tracker.records}) == 2
        assert [record.status for record in tracker.records] == ["completed", "cancelled"]
        assert tracker.records[0].usage.is_final
        assert not tracker.records[1].usage.is_final
        assert session.usage_summary() == tracker.snapshot()
        assert tracker.snapshot().input_tokens == 300
        assert tracker.snapshot().output_tokens == 53
        assert tracker.snapshot().total_tokens == 353
        expected = session.usage_summary()
        session_id = session.session_id
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session.close()

    new_tracker = RunUsageTracker()
    restored = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=new_tracker)
    try:
        restored.resume_session(session_id)
        assert restored.usage_summary() == expected
        assert restored.get_compaction(activity.id).status == "cancelled"
        assert restored.transcript == original
        assert new_tracker.snapshot().request_count == 0
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_cancel_on_first_summary_delta_closes_provider_before_returning(
    openai_provider_config, tmp_path, monkeypatch,
):
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, cwd=tmp_path, usage_tracker=tracker)
    _long_history(session)
    original = deepcopy(session.transcript)
    token = CancellationToken()
    session_closed = False
    callbacks = []

    class CancelOnFirstDeltaProvider:
        def __init__(self):
            self.requests = []
            self.finalized = False
            self.resumed_after_yield = False

        @complete_test_response
        async def stream_chat(self, request):
            self.requests.append(request)
            assert request.purpose == "compaction"
            assert request.cancellation_token is token
            try:
                # 消费者在拿到第一帧后检查取消；生成器此时正停在 yield。
                token.cancel()
                yield StreamEvent(kind="text_delta", text=_summary())
                self.resumed_after_yield = True
            finally:
                self.finalized = True

    provider = CancelOnFirstDeltaProvider()
    original_record_usage = session._sessions.record_usage

    def observe_callback(data, **scope):
        callbacks.append((data["status"], provider.finalized, session_closed))
        assert not session_closed, "Session 关闭后不应再收到用量回调。"
        original_record_usage(data, **scope)

    # 仅观察持久化出口，实际请求与取消对象均走 Controller 的公开接口。
    monkeypatch.setattr(session._sessions, "record_usage", observe_callback)

    try:
        with pytest.raises(asyncio.CancelledError):
            await session.compact_context(
                provider=provider, visible_tools=[], cancellation_token=token,
            )

        # 无需下一轮事件循环来替消费者清理，返回取消前必须完整收尾。
        assert provider.finalized
        assert not provider.resumed_after_yield
        assert len(provider.requests) == 1
        assert callbacks == [("running", False, False), ("cancelled", True, False)]
        assert len(session.state.compaction_activities) == 1
        activity = next(iter(session.state.compaction_activities.values()))
        assert session.get_compaction(activity.id).status == "cancelled"
        assert session.transcript == original
        assert len(tracker.records) == 1
        record = tracker.records[0]
        assert record.status == "cancelled"
        assert record.purpose == "compaction"
        assert record.session_id == session.session_id
        assert not record.usage.is_final
        assert record.usage.input_tokens is None
        assert record.usage.output_tokens is None
        assert session.state.request_usage[record.request_id] == record.to_dict()
        assert session.usage_summary() == tracker.snapshot()
        expected_callbacks = list(callbacks)
        expected_records = deepcopy(session.state.request_usage)
    finally:
        session.close()

    session_closed = True
    # 允许此前可能排队的异步生成器终结器执行，不能把它留到会话关闭后。
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert callbacks == expected_callbacks
    assert session.state.request_usage == expected_records
    assert tracker.records[0].status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("text,stop_reason", [("无效摘要", None), (_summary(), "length")])
@pytest.mark.parametrize("matching_shape", [True, False])
async def test_rejected_summary_retains_history_and_anchor_but_accounts_usage(
    openai_provider_config, text, stop_reason, matching_shape,
):
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, usage_tracker=tracker)
    _long_history(session)
    request = session.build_request([], allow_tool_calls=matching_shape)
    session.update_context_usage(request, MessageUsage(input_tokens=20_000, output_tokens=100))
    previous_transcript = deepcopy(session.transcript)
    previous_anchor = deepcopy(session.context_state.usage_anchor)
    provider = SummaryProvider(text, stop_reason=stop_reason)

    with pytest.raises(ContextCompactionError):
        await session.compact_context(provider=provider, visible_tools=[], turn_id="turn")

    assert session.transcript == previous_transcript
    assert session.context_state.usage_anchor == previous_anchor
    expected_requests = 2 if stop_reason is None else 1
    assert session.usage_summary().total_tokens == tracker.snapshot().total_tokens == 150 * expected_requests
    assert len(provider.requests) == tracker.snapshot().request_count == expected_requests
    assert len({record.request_id for record in tracker.records}) == expected_requests
    assert all(record.purpose == "compaction" and record.turn_id == "turn" and record.message_id is None
               for record in tracker.records)


@pytest.mark.asyncio
async def test_complete_candidate_including_tools_rejects_summary_and_rolls_back(openai_provider_config):
    openai_provider_config.context_window = 8_000
    session = SessionController(openai_provider_config)
    session.create_user_message("旧材料" + "x" * 5_000)
    reply = session.create_assistant_message()
    session.append_message_content(reply.id, "已记录。")
    session.complete_message(reply.id)
    session.create_user_message("继续")
    previous = deepcopy(session.transcript)
    context = deepcopy(session.context_state)
    # 摘要正文能缩小，但完整请求的工具定义本身已装不进窗口。
    tools = [ToolDefinition(name="large_tool", description="x" * 25_000,
                            input_schema={"type": "object"})]
    provider = SummaryProvider(_summary())

    with pytest.raises(ContextCompactionError):
        await session.compact_context(provider=provider, visible_tools=tools)

    assert session.transcript == previous
    assert session.context_state == context
    assert session.usage_summary().total_tokens == 150


def test_context_details_are_read_only_for_runtime_anchor(openai_provider_config):
    session = SessionController(openai_provider_config)
    session.create_user_message("开始")
    request = session.build_request([], allow_tool_calls=True)
    session.update_context_usage(request, MessageUsage(input_tokens=1_000, output_tokens=20))
    anchor = deepcopy(session.context_state.usage_anchor)

    other_shape = session.build_request([], allow_tool_calls=False)
    assert session.context_estimate(other_shape).source == "estimated"
    assert session.context_state.usage_anchor == anchor
    assert session.context_estimate(request).tokens == 1_000
    assert session.context_estimate(request).source == "usage_calibrated"


def test_thinking_output_cap_and_input_budget_share_actual_allowance(claude_provider_config):
    claude_provider_config.context_window = 16_000
    claude_provider_config.thinking = ThinkingConfig(enabled=True, budget_tokens=8_000)
    session = SessionController(claude_provider_config)
    request = session.build_request([], allow_tool_calls=True)
    budget = context_budget(session.context_window, request.max_output_tokens)

    assert request.max_output_tokens > request.thinking.budget_tokens
    assert budget.output_tokens == request.max_output_tokens
    assert budget.input_limit + budget.output_tokens < session.context_window

    claude_provider_config.context_window = 4_000
    request = session.build_request([], allow_tool_calls=True)
    assert context_budget(session.context_window, request.max_output_tokens).input_limit == 0


def test_default_thinking_budget_is_shared_by_capacity_and_provider(claude_provider_config):
    claude_provider_config.context_window = 8_000
    claude_provider_config.thinking = ThinkingConfig(enabled=True)
    session = SessionController(claude_provider_config)
    request = session.build_request([], allow_tool_calls=True)
    payload = ClaudeProvider(claude_provider_config)._build_payload(request)

    assert payload['thinking']['budget_tokens'] == request.thinking.effective_budget_tokens == 2_048
    assert payload['max_tokens'] == request.max_output_tokens > 2_048


@pytest.mark.asyncio
async def test_rejected_full_candidate_counts_failures_on_restored_context(openai_provider_config, tmp_path):
    openai_provider_config.context_window = 8_000
    session = SessionController(openai_provider_config)
    session.create_user_message("旧材料" + "x" * 5_000)
    reply = session.create_assistant_message()
    session.append_message_content(reply.id, "已记录。")
    session.complete_message(reply.id)

    class LargeTool:
        definition = ToolDefinition(name="large_tool", description="x" * 25_000,
                                    input_schema={"type": "object"})

    registry = ToolRegistry()
    registry.register(LargeTool())
    provider = SummaryProvider(_summary())
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=tmp_path))

    for count in range(1, 4):
        events = [event async for event in runner.run_user_turn(f"继续 {count}")]
        assert events[-1].kind == "turn_failed"
        assert session.context_state.automatic_failure_count == count

    assert session.context_state.automatic_compaction_disabled is True
    assert len(provider.requests) == 3


@pytest.mark.parametrize("old_format", ["missing_ledger", "old_context"])
def test_old_accounting_format_cannot_silently_become_zero_usage(openai_provider_config, old_format):
    session = SessionController(openai_provider_config)
    session.create_user_message("保留旧记录")
    snapshot = session._snapshot()
    if old_format == "missing_ledger":
        del snapshot['state']['request_usage']
    else:
        snapshot['state']['context_management']['version'] = 1

    with pytest.raises(SessionRepositoryError, match="不兼容"):
        SessionCodec.decode(snapshot, session.session_id)
