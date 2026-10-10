from __future__ import annotations

import asyncio

import pytest

from lancher_code.errors import ContextCompactionError, ProviderPromptTooLongError
from lancher_code.models import MessageUsage, StreamEvent
from lancher_code.session import SessionController
from lancher_code.sessions import SessionRepositoryError
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import TurnRunner


def _summary(*, usage=None):
    headings = ("主要请求和意图", "关键技术概念", "文件和代码段", "错误与修复", "问题解决过程",
                "用户消息与明确反馈", "待办任务", "当前工作", "可能的下一步")
    return [StreamEvent(kind="text_delta", text="<summary>" + "\n".join(
        f"## {heading}\n已记录" for heading in headings) + "</summary>"),
        StreamEvent(kind="message_end", usage=usage if usage is not None else MessageUsage())]


def _invalid_summary():
    return [StreamEvent(kind="text_delta", text="缺少约定的摘要结构"), StreamEvent(kind="message_end",
            usage=MessageUsage(input_tokens=100, output_tokens=50, cached_input_tokens=0))]


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.entered = asyncio.Event()

    async def stream_chat(self, request):
        self.requests.append(request)
        self.entered.set()
        response = self.responses.pop(0)
        if response is None:
            await asyncio.Event().wait()
        elif isinstance(response, BaseException):
            raise response
        else:
            for event in response:
                yield event


def _runner(config, root, responses, *, history_size=50_000):
    session = SessionController(config, cwd=root)
    session.create_user_message("旧任务资料：" + "x" * history_size)
    old = session.create_assistant_message()
    session.append_message_content(old.id, "此前资料已处理")
    session.complete_message(old.id)
    session.create_user_message("继续分析下一项")
    registry = ToolRegistry()
    provider = Provider(responses)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=root))
    return runner, session, provider


async def _observe(events, event):
    events.append(event)


@pytest.mark.asyncio
async def test_manual_precreated_activity_keeps_id_and_internal_retry_does_not_add_activity(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        ProviderPromptTooLongError("summary input too long"), _summary(),
    ])
    activity = session.begin_compaction("manual")
    events = []
    result = await runner.compact_context(activity_id=activity.id,
                                          on_activity=lambda event: _observe(events, event))
    assert len(provider.requests) == 2
    assert result.after_tokens < result.before_tokens
    assert [event.compaction.status for event in events] == ["running", "completed"]
    assert {event.compaction.id for event in events} == {activity.id}
    assert session.get_compaction(activity.id).status == "completed"
    assert not runner.is_compacting
    session.close()


@pytest.mark.asyncio
async def test_format_retry_uses_two_accounted_requests_and_completes_same_manual_activity(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        _invalid_summary(), _summary(usage=MessageUsage(input_tokens=200, output_tokens=60,
                                                       cached_input_tokens=0)),
    ])
    activity = session.begin_compaction("manual")
    events = []
    try:
        result = await runner.compact_context(activity_id=activity.id,
                                              on_activity=lambda event: _observe(events, event))
        assert result.after_tokens < result.before_tokens
        assert [event.compaction.status for event in events] == ["running", "completed"]
        assert {event.compaction.id for event in events} == {activity.id}
        assert len(session.state.compaction_activities) == 1
        assert len(provider.requests) == 2
        first, retry = provider.requests
        assert first.request_id != retry.request_id
        assert first.messages[:-1] == retry.messages[:-1]
        assert first.messages[-1].role == retry.messages[-1].role == "user"
        assert first.messages[-1] != retry.messages[-1]
        assert first.max_output_tokens == retry.max_output_tokens
        assert first.cancellation_token is retry.cancellation_token
        assert all(request.purpose == "compaction" and not request.allow_tool_calls
                   and request.tools == [] and request.thinking is None for request in provider.requests)
        assert session.usage_summary().request_count == 2
        assert session.usage_summary().total_tokens == session._usage_tracker.snapshot().total_tokens == 410
    finally:
        session.close()


@pytest.mark.asyncio
async def test_two_bad_format_attempts_count_as_one_automatic_failure_and_continue_once(
    openai_provider_config, tmp_path,
):
    openai_provider_config.context_window = 34_000
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        _invalid_summary(), _invalid_summary(),
        [StreamEvent(kind="text_delta", text="继续完成本轮"), StreamEvent(kind="message_end",
                     usage=MessageUsage(input_tokens=10, output_tokens=2, cached_input_tokens=0))],
    ], history_size=78_000)
    try:
        events = [event async for event in runner.run_user_turn("继续分析" + "x" * 1_000)]
        activities = [event.compaction for event in events if event.kind == "compaction_updated"]
        assert events[-1].kind == "turn_completed"
        assert [activity.status for activity in activities] == ["running", "failed"]
        assert len({activity.id for activity in activities}) == 1
        assert activities[-1].continued
        assert len(session.state.compaction_activities) == 1
        assert session.context_state.automatic_failure_count == 1
        assert not session.context_state.automatic_compaction_disabled
        assert [request.purpose for request in provider.requests] == ["compaction", "compaction", "chat"]
        assert len({request.request_id for request in provider.requests}) == 3
        assert session.usage_summary().total_tokens == 312
    finally:
        session.close()


@pytest.mark.asyncio
async def test_invalid_manual_summary_reports_failure_and_keeps_original_context(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        _invalid_summary(), _invalid_summary(),
    ])
    original = session.transcript
    events = []
    with pytest.raises(ContextCompactionError):
        await runner.compact_context(on_activity=lambda event: _observe(events, event))
    assert session.transcript == original
    assert [event.compaction.status for event in events] == ["running", "failed"]
    assert len({event.compaction.id for event in events}) == 1
    assert events[-1].compaction.error_text
    assert not events[-1].compaction.continued
    assert not runner.is_compacting
    assert len(provider.requests) == 2
    assert session.usage_summary().total_tokens == 300
    session.close()


@pytest.mark.asyncio
async def test_cancel_manual_summary_terminates_same_activity_and_releases_operation_guard(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [None])
    events = []
    task = asyncio.create_task(runner.compact_context(on_activity=lambda event: _observe(events, event)))
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        assert runner.is_compacting
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [event.compaction.status for event in events] == ["running", "cancelled"]
        assert events[0].compaction.id == events[1].compaction.id
        assert session.get_compaction(events[0].compaction.id).status == "cancelled"
        assert not runner.is_compacting
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
async def test_rejected_second_manual_operation_finishes_precreated_activity_without_releasing_first(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [None])
    first = asyncio.create_task(runner.compact_context())
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        prepared = session.begin_compaction("manual")
        events = []
        with pytest.raises(ContextCompactionError, match="模型正在响应"):
            await runner.compact_context(activity_id=prepared.id,
                                          on_activity=lambda event: _observe(events, event))
        assert len(events) == 1
        assert events[0].compaction.id == prepared.id
        assert events[0].compaction.status == "failed"
        assert runner.is_compacting
        assert not first.done()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
async def test_terminal_activity_write_failure_still_finishes_ui_wait_and_propagates_save_error(
    openai_provider_config, tmp_path, monkeypatch,
):
    runner, session, _ = _runner(openai_provider_config, tmp_path, [
        _invalid_summary(), _invalid_summary(),
    ])
    record = session._sessions.record_compaction

    def reject_terminal(data):
        if data["status"] == "failed":
            raise SessionRepositoryError("模拟活动终态写入失败")
        return record(data)

    events = []
    with monkeypatch.context() as patch:
        patch.setattr(session._sessions, "record_compaction", reject_terminal)
        with pytest.raises(SessionRepositoryError, match="终态写入失败"):
            await runner.compact_context(on_activity=lambda event: _observe(events, event))
    assert [event.compaction.status for event in events] == ["running", "failed"]
    assert events[0].compaction.id == events[1].compaction.id
    assert not runner.is_compacting
    session.close()


@pytest.mark.asyncio
async def test_cancel_automatic_summary_sends_terminal_activity_before_cancelled_turn(
    openai_provider_config, tmp_path,
):
    openai_provider_config.context_window = 34_000
    runner, session, provider = _runner(openai_provider_config, tmp_path, [None], history_size=78_000)
    events = []

    async def collect():
        async for event in runner.run_user_turn("继续自动整理" + "x" * 1_000):
            events.append(event)
            if event.kind == "compaction_updated" and event.compaction.status == "running":
                runner.cancel_active_turn()

    await asyncio.wait_for(collect(), 10)
    activities = [event for event in events if event.kind == "compaction_updated"]
    assert [event.compaction.status for event in activities] == ["running", "cancelled"]
    assert activities[0].compaction.id == activities[1].compaction.id
    assert events[-1].kind == "turn_cancelled"
    assert all(event.message.id == events[-1].message.id for event in activities)
    assert session.get_compaction(activities[0].compaction.id).status == "cancelled"
    assert len(provider.requests) <= 1
    session.close()


@pytest.mark.asyncio
async def test_automatic_persistence_failure_is_not_reported_as_completed_or_continued(
    openai_provider_config, tmp_path, monkeypatch,
):
    openai_provider_config.context_window = 34_000
    runner, session, provider = _runner(openai_provider_config, tmp_path, [], history_size=78_000)

    async def fail_persistence(**_kwargs):
        raise SessionRepositoryError("模拟候选上下文保存失败")

    monkeypatch.setattr(session, "compact_context", fail_persistence)
    events = [event async for event in runner.run_user_turn("继续自动整理" + "x" * 1_000)]
    activities = [event.compaction for event in events if event.kind == "compaction_updated"]
    assert [activity.status for activity in activities] == ["running", "failed"]
    assert activities[0].id == activities[1].id
    assert not activities[-1].continued
    assert "保存失败" in activities[-1].error_text
    assert events[-1].kind == "turn_failed"
    assert provider.requests == []
    assert session.context_state.automatic_failure_count == 0
    session.close()


@pytest.mark.asyncio
async def test_automatic_failure_above_hard_budget_stops_without_continued_flag(
    openai_provider_config, tmp_path,
):
    openai_provider_config.context_window = 34_000
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        _invalid_summary(), _invalid_summary(),
    ], history_size=90_000)
    events = [event async for event in runner.run_user_turn("当前请求" + "x" * 1_000)]
    activities = [event.compaction for event in events if event.kind == "compaction_updated"]
    assert [activity.status for activity in activities] == ["running", "failed"]
    assert not activities[-1].continued
    assert events[-1].kind == "turn_failed"
    assert "自动压缩失败" in events[-1].error_text
    assert len(provider.requests) == 2
    assert all(request.purpose == "compaction" for request in provider.requests)
    assert session.usage_summary().total_tokens == 300
    session.close()


@pytest.mark.asyncio
async def test_emergency_summary_failure_keeps_same_activity_and_original_overflow_error(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        ProviderPromptTooLongError("模型拒绝本轮超限请求"),
        _invalid_summary(), _invalid_summary(),
    ])
    events = [event async for event in runner.run_user_turn("继续任务")]
    activities = [event.compaction for event in events if event.kind == "compaction_updated"]
    assert [activity.status for activity in activities] == ["running", "failed"]
    assert len({activity.id for activity in activities}) == 1
    assert not activities[-1].continued
    assert activities[-1].error_text
    assert events[-1].kind == "turn_failed"
    assert events[-1].error_text == "模型拒绝本轮超限请求"
    assert len(provider.requests) == 3
    assert session.usage_summary().total_tokens == 300
    session.close()


@pytest.mark.asyncio
async def test_emergency_recovery_still_retries_main_request_only_once(
    openai_provider_config, tmp_path,
):
    runner, session, provider = _runner(openai_provider_config, tmp_path, [
        ProviderPromptTooLongError("第一次超限"), _summary(), ProviderPromptTooLongError("第二次超限"),
    ])
    events = [event async for event in runner.run_user_turn("继续任务")]
    activities = [event.compaction for event in events if event.kind == "compaction_updated"]
    assert [activity.status for activity in activities] == ["running", "completed"]
    assert len({activity.id for activity in activities}) == 1
    assert activities[0].trigger == "emergency"
    assert len(provider.requests) == 3
    assert events[-1].kind == "turn_failed"
    assert events[-1].error_text == "第二次超限"
    session.close()
