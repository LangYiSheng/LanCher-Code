from __future__ import annotations

import copy
import asyncio
from datetime import datetime, timezone

import pytest

from lancher_code.context_management import SUMMARY_HEADINGS
from lancher_code.errors import ContextCompactionError, ProviderPromptTooLongError
from lancher_code.models import ContextCompactionResult, MessageUsage, SessionState, StreamEvent
from lancher_code.session import SessionController
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.repository import ProjectSessionRepository, SessionRepositoryError
from lancher_code.sessions.service import SessionService
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import TurnRunner


class SummaryProvider:
    def __init__(self, *, retry=False, invalid=False):
        self.requests = []
        self.retry = retry
        self.invalid = invalid

    async def stream_chat(self, request):
        self.requests.append(request)
        if self.retry and len(self.requests) == 1:
            raise ProviderPromptTooLongError('供应商拒绝第一次摘要请求')
        summary = '<summary>' + '\n'.join(f'## {heading}\n已整理。' for heading in SUMMARY_HEADINGS) + '</summary>'
        yield StreamEvent(kind='text_delta', text='无效摘要' if self.invalid else summary)
        yield StreamEvent(kind='message_end', usage=MessageUsage(input_tokens=100, output_tokens=50))


def long_history(controller):
    for index in range(3):
        controller.create_user_message(f'旧材料{index}' + 'x' * 16000)
        reply = controller.create_assistant_message()
        controller.append_message_content(reply.id, '材料已记录。')
        controller.complete_message(reply.id)
    controller.create_user_message('继续，并保留本条原话。')


def test_empty_manual_activity_does_not_create_session(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        activity = controller.begin_compaction('manual')
        assert activity.status == 'running'
        assert activity.after_message_id is None
        assert activity.before_tokens is activity.before_source is None
        ended = controller.finish_compaction(activity.id, status='failed', error_text='没有可压缩内容')
        assert ended.status == 'failed' and ended.finished_at is not None
        assert activity.status == 'running'
        assert controller.session_id is None
        assert controller.paths is None
        assert controller.list_sessions() == []
    finally:
        controller.close()
    assert not (tmp_path / '.lancher' / 'sessions').exists()


@pytest.mark.parametrize('trigger', ['manual', 'automatic', 'emergency'])
def test_start_save_failure_finishes_same_in_memory_activity_without_retry(
    openai_provider_config, tmp_path, monkeypatch, trigger,
):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        controller.create_user_message('处理请求')
        message = controller.create_assistant_message()
        before = copy.deepcopy(controller.transcript)
        flush_count = 0

        def failing_flush(**kwargs):
            nonlocal flush_count
            flush_count += 1
            raise SessionRepositoryError('无法保存压缩启动状态')

        with monkeypatch.context() as patch:
            patch.setattr(controller, 'flush', failing_flush)
            with pytest.raises(SessionRepositoryError, match='无法保存'):
                controller.begin_compaction(trigger, message_id=message.id if trigger != 'manual' else None)
        assert flush_count == 1
        assert controller.transcript == before
        assert len(controller.state.compaction_activities) == 1
        activity = next(iter(controller.state.compaction_activities.values()))
        assert activity.status == 'failed'
        assert activity.finished_at is not None
        assert activity.error_text == '无法保存压缩启动状态'
        if trigger != 'manual':
            assert message.trace.entries[-1].metadata['activity_id'] == activity.id
    finally:
        controller.close()


@pytest.mark.asyncio
async def test_automatic_start_save_failure_does_not_leave_spinner_after_turn_failure(
    openai_provider_config, tmp_path, monkeypatch,
):
    openai_provider_config.context_window = 8192
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    provider = SummaryProvider()
    registry = ToolRegistry()
    runner = TurnRunner(provider, controller, registry, ToolExecutor(registry, cwd=tmp_path))
    try:
        long_history(controller)
        original_flush = controller.flush
        failed_starts = 0

        def fail_only_running_activity(**kwargs):
            nonlocal failed_starts
            if any(activity.status == 'running' for activity in controller.state.compaction_activities.values()):
                failed_starts += 1
                raise SessionRepositoryError('压缩开始写盘失败')
            return original_flush(**kwargs)

        monkeypatch.setattr(controller, 'flush', fail_only_running_activity)
        events = await asyncio.wait_for(
            _collect_turn(runner, '继续检查结果'), timeout=5,
        )
        assert failed_starts == 1
        assert not runner.has_active_turn
        assert not provider.requests
        assert any(event.kind == 'turn_failed' for event in events)
        activity = next(iter(controller.state.compaction_activities.values()))
        assert activity.trigger == 'automatic' and activity.status == 'failed'
        assert activity.finished_at is not None
        assert activity.error_text == '压缩开始写盘失败'
        failed_message = next(event.message for event in events if event.kind == 'turn_failed')
        assert any(entry.kind == 'compaction' and entry.metadata.get('activity_id') == activity.id
                   for entry in failed_message.trace.entries)
    finally:
        controller.close()


async def _collect_turn(runner, text):
    return [event async for event in runner.run_user_turn(text)]


def test_activity_is_display_trace_only_and_terminal_updates_are_idempotent(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        controller.create_user_message('处理请求')
        message = controller.create_assistant_message()
        controller.append_message_content(message.id, '先整理材料。')
        before = copy.deepcopy(controller.transcript)
        activity = controller.begin_compaction('automatic', message_id=message.id, turn_id='turn-1')
        assert controller.transcript == before
        assert message.trace.entries[-1].kind == 'compaction'
        assert message.trace.entries[-1].metadata == {'activity_id': activity.id}
        assert message.trace.entries[-2].metadata['state'] == 'complete'
        failed = controller.finish_compaction(activity.id, status='failed', error_text='摘要失败')
        count = len(ProjectSessionRepository(tmp_path).read(controller.session_id))
        assert controller.finish_compaction(activity.id, status='cancelled') == failed
        assert len(ProjectSessionRepository(tmp_path).read(controller.session_id)) == count
        continued = controller.finish_compaction(activity.id, status='failed', continued=True)
        assert continued.continued and continued.finished_at == failed.finished_at
        assert not failed.continued
        assert activity.status == 'running'
        count = len(ProjectSessionRepository(tmp_path).read(controller.session_id))
        assert controller.finish_compaction(activity.id, status='failed', continued=True) == continued
        assert len(ProjectSessionRepository(tmp_path).read(controller.session_id)) == count
        snapshot = controller.get_compaction(activity.id)
        snapshot.error_text = '外部修改副本'
        assert controller.get_compaction(activity.id).error_text == '摘要失败'
    finally:
        controller.close()


@pytest.mark.parametrize('segment_kind', ['text', 'thinking', None])
def test_start_event_alone_restores_assistant_trace_position_without_duplicate_marker(
    openai_provider_config, tmp_path, segment_kind,
):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        controller.create_user_message('恢复压缩活动的位置')
        message = controller.create_assistant_message()
        if segment_kind == 'text':
            controller.append_message_content(message.id, '整理旧材料。')
        elif segment_kind == 'thinking':
            controller.append_trace_thinking(message.id, '先查看上下文。')
        controller.flush()
        activity = controller.begin_compaction('automatic', message_id=message.id, turn_id='turn-start')
        events = ProjectSessionRepository(tmp_path).read(controller.session_id)
        start_index = next(index for index, event in enumerate(events)
                           if event['type'] == 'compaction.updated' and event['data']['id'] == activity.id)
        projected = SessionCodec.project(events[:start_index + 1])
        owner = next(item for item in projected['messages'] if item['id'] == message.id)
        assert owner['trace'] == controller._snapshot()['messages'][-1]['trace']
        assert owner['trace']['entries'][-1]['kind'] == 'compaction'
        assert owner['trace']['entries'][-1]['metadata']['activity_id'] == activity.id
        if segment_kind:
            assert owner['trace']['entries'][-2]['metadata']['state'] == 'complete'
        state, transcript, _, _ = SessionCodec.decode(projected, controller.session_id)
        SessionController._recover_interrupted_history(state, transcript)
        interrupted = state.compaction_activities[activity.id]
        assert interrupted.status == 'interrupted' and interrupted.finished_at is None
        restored_message = next(item for item in state.messages if item.id == message.id)
        assert sum(entry.kind == 'compaction' and entry.metadata.get('activity_id') == activity.id
                   for entry in restored_message.trace.entries) == 1
        # 后续完整消息更新覆盖相同轨迹，终态活动事件也不再追加第二个引用。
        controller.finish_compaction(activity.id, status='cancelled')
        all_events = ProjectSessionRepository(tmp_path).read(controller.session_id)
        final_projection = SessionCodec.project(all_events)
        assert final_projection == controller._snapshot()
        entries = final_projection['messages'][-1]['trace']['entries']
        assert sum(entry['kind'] == 'compaction' and entry['metadata'].get('activity_id') == activity.id
                   for entry in entries) == 1
    finally:
        controller.close()


@pytest.mark.asyncio
async def test_context_and_completed_activity_commit_in_same_final_event(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        long_history(controller)
        before = copy.deepcopy(controller.transcript)
        request = controller.build_request([], allow_tool_calls=True)
        controller.update_context_usage(request, MessageUsage(input_tokens=30000, output_tokens=100))
        activity = controller.begin_compaction('manual', turn_id='manual-turn')
        controller._sessions.checkpoint()
        result = await controller.compact_context(provider=SummaryProvider(retry=True), visible_tools=[],
                                                 activity_id=activity.id, turn_id='manual-turn')
        finished = controller.get_compaction(activity.id)
        assert finished.status == 'completed'
        assert finished.before_tokens == result.before_tokens == 30000
        assert finished.before_source == result.before_source == 'usage_calibrated'
        assert finished.after_tokens == result.after_tokens < finished.before_tokens
        assert finished.after_source == 'estimated'
        assert finished.dropped_groups > 0
        assert finished.after_message_id == controller.state.messages[-1].id
        assert len(controller.state.compaction_activities) == 1
        assert controller.usage_summary().request_count == 2

        events = ProjectSessionRepository(tmp_path).read(controller.session_id)
        assert events[-1]['type'] == 'context.compacted'
        boundary = events[-1]['data']
        assert boundary['activity_id'] == activity.id
        assert boundary['compaction']['status'] == 'completed'
        assert boundary['compaction']['before_source'] == 'usage_calibrated'
        assert not any(event['type'] == 'compaction.updated' and event['data']['status'] == 'completed'
                       for event in events)
        expected = controller._snapshot()
        assert SessionCodec.project(events) == expected
        completed_state, completed_transcript, _, _ = SessionCodec.decode(SessionCodec.project(events), controller.session_id)
        assert completed_state.compaction_activities[activity.id].status == 'completed'
        assert completed_transcript == controller.transcript
        assert completed_state.context_management.usage_anchor is None

        # 中断在原子提交前：原上下文和 running；提交后：新上下文和 completed。
        pending_state, pending_transcript, _, _ = SessionCodec.decode(SessionCodec.project(events[:-1]), controller.session_id)
        assert pending_transcript == before
        assert pending_state.compaction_activities[activity.id].status == 'running'
        SessionController._recover_interrupted_history(pending_state, pending_transcript)
        assert pending_state.compaction_activities[activity.id].status == 'interrupted'
        assert '保留原上下文' not in pending_state.compaction_activities[activity.id].error_text
        count = len(events)
        assert controller.finish_compaction(activity.id, status='completed', result=result) == finished
        assert len(ProjectSessionRepository(tmp_path).read(controller.session_id)) == count
        session_id = controller.session_id
        controller._sessions.close()

        # checkpoint 早于压缩，从尾事件恢复的完成状态仍与全量重放一致。
        service = SessionService(tmp_path)
        prepared = service.prepare(session_id)
        try:
            assert prepared[1] == expected
            assert prepared[2][0].compaction_activities[activity.id] == finished
        finally:
            prepared[0].close()
    finally:
        controller.close()


@pytest.mark.asyncio
async def test_failed_summary_preserves_history_and_never_commits_completed_activity(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        long_history(controller)
        message = controller.create_assistant_message()
        activity = controller.begin_compaction('automatic', message_id=message.id, turn_id='turn')
        before = copy.deepcopy(controller.transcript)
        with pytest.raises(ContextCompactionError):
            await controller.compact_context(provider=SummaryProvider(invalid=True), visible_tools=[], activity_id=activity.id)
        controller.finish_compaction(activity.id, status='failed', error_text='摘要无效', continued=True)
        assert controller.transcript == before
        finished = controller.get_compaction(activity.id)
        assert finished.status == 'failed' and finished.continued
        assert finished.before_tokens is not None and finished.before_source == 'estimated'
        assert finished.after_tokens is finished.after_source is None
        assert controller.usage_summary().total_tokens == 300
        assert controller.usage_summary().request_count == 2
        events = ProjectSessionRepository(tmp_path).read(controller.session_id)
        assert not any(event['type'] == 'context.compacted' for event in events)
        assert SessionCodec.project(events) == controller._snapshot()
    finally:
        controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_direct_compaction_api_owns_failed_and_cancelled_activity_lifecycle(openai_provider_config, tmp_path, cancelled):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        long_history(controller)
        before = copy.deepcopy(controller.transcript)

        class CancelledProvider:
            async def stream_chat(self, request):
                raise asyncio.CancelledError
                yield  # pragma: no cover

        provider = CancelledProvider() if cancelled else SummaryProvider(invalid=True)
        with pytest.raises(asyncio.CancelledError if cancelled else ContextCompactionError):
            await controller.compact_context(provider=provider, visible_tools=[])
        assert controller.transcript == before
        assert len(controller.state.compaction_activities) == 1
        activity = next(iter(controller.state.compaction_activities.values()))
        assert activity.status == ('cancelled' if cancelled else 'failed')
        assert activity.finished_at is not None
        assert activity.after_tokens is None
        assert SessionCodec.project(ProjectSessionRepository(tmp_path).read(controller.session_id)) == controller._snapshot()
    finally:
        controller.close()


@pytest.mark.asyncio
async def test_failure_writing_compaction_boundary_rolls_back_candidate_and_activity(openai_provider_config, tmp_path, monkeypatch):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        long_history(controller)
        activity = controller.begin_compaction('manual')
        before = copy.deepcopy(controller.transcript)
        context = copy.deepcopy(controller.context_state)
        original_append = controller._sessions.writer.append

        def failing_append(kind, data, **kwargs):
            if kind == 'context.compacted':
                raise SessionRepositoryError('模拟提交前磁盘失败')
            return original_append(kind, data, **kwargs)

        monkeypatch.setattr(controller._sessions.writer, 'append', failing_append)
        with pytest.raises(SessionRepositoryError, match='磁盘失败'):
            await controller.compact_context(provider=SummaryProvider(), visible_tools=[], activity_id=activity.id)
        assert controller.transcript == before and controller.context_state == context
        assert controller.get_compaction(activity.id).status == 'running'
        controller.finish_compaction(activity.id, status='failed', error_text='磁盘失败')
        events = ProjectSessionRepository(tmp_path).read(controller.session_id)
        decoded, transcript, _, _ = SessionCodec.decode(SessionCodec.project(events), controller.session_id)
        assert transcript == before
        assert decoded.compaction_activities[activity.id].status == 'failed'
        assert controller.usage_summary().total_tokens == 150
    finally:
        controller.close()


@pytest.mark.parametrize('with_checkpoint', [False, True])
@pytest.mark.parametrize('trigger', ['manual', 'automatic', 'emergency'])
def test_restore_running_activity_as_durable_interrupted_without_claiming_rollback(
    openai_provider_config, tmp_path, with_checkpoint, trigger,
):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message('恢复前的请求')
    message = controller.create_assistant_message()
    controller.append_message_content(message.id, '整理材料。')
    activity = controller.begin_compaction(trigger, message_id=message.id if trigger != 'manual' else None,
                                           turn_id='previous-turn')
    if with_checkpoint:
        controller._sessions.checkpoint()
    session_id = controller.session_id
    controller._sessions.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        repaired = restored.get_compaction(activity.id)
        assert repaired.status == 'interrupted'
        assert repaired.finished_at is None
        assert repaired.after_tokens is repaired.after_source is None
        assert repaired.error_text == '上次压缩未完整收尾，请以恢复后的实际上下文为准。'
        events = ProjectSessionRepository(tmp_path).read(session_id)
        updates = [event for event in events if event['type'] == 'compaction.updated' and event['data']['id'] == activity.id]
        assert [event['data']['status'] for event in updates] == ['running', 'interrupted']
        assert updates[-1]['turn_id'] == 'previous-turn'
        assert SessionCodec.project(events) == restored._snapshot()
        restored._sessions.close()
    finally:
        restored.close()
    service = SessionService(tmp_path)
    prepared = service.prepare(session_id)
    try:
        assert prepared[2][0].compaction_activities[activity.id].status == 'interrupted'
    finally:
        prepared[0].close()


class MemoryWriter:
    def __init__(self, initial):
        self.events = [{'type': 'session.created', 'data': {'initial_data': copy.deepcopy(initial)}}]

    def append(self, kind, data, turn_id=None):
        self.events.append({'type': kind, 'data': copy.deepcopy(data), 'turn_id': turn_id})


def test_activity_updates_are_incremental_and_unrelated_state_does_not_rewrite_history():
    initial = SessionCodec.encode(SessionState(), [], [], None)
    service = SessionService.__new__(SessionService)
    service.writer = MemoryWriter(initial)
    service._saved = copy.deepcopy(initial)
    snapshot = copy.deepcopy(initial)
    for index in range(40):
        activity = {
            'id': f'{index:032x}', 'trigger': 'manual', 'status': 'failed',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'finished_at': datetime.now(timezone.utc).isoformat(),
            'message_id': None, 'after_message_id': None, 'turn_id': None,
            'before_tokens': None, 'after_tokens': None, 'before_source': None, 'after_source': None,
            'dropped_groups': 0, 'error_text': '没有可压缩内容', 'continued': False,
        }
        snapshot['state']['compaction_activities'][activity['id']] = activity
        snapshot['state']['plan_mode_turn_count'] = index + 1
        service.persist(snapshot)
        service.persist(snapshot)
    updates = [event for event in service.writer.events if event['type'] == 'compaction.updated']
    assert len(updates) == 40
    assert all('compaction_activities' not in event['data'] for event in service.writer.events
               if event['type'] == 'state.changed')
    assert SessionCodec.project(service.writer.events) == snapshot == service._saved


@pytest.mark.parametrize('changes', [
    {'before_tokens': -1, 'before_source': 'estimated'},
    {'before_tokens': True, 'before_source': 'estimated'},
    {'before_tokens': 2, 'before_source': None},
    {'before_tokens': 2, 'before_source': 'usage'},
    {'after_tokens': 1, 'after_source': 'estimated'},
    {'started_at': '2026-10-10T12:00:00'},
    {'finished_at': '2026-10-10T12:00:00+00:00'},
    {'status': 'completed'},
    {'dropped_groups': True},
    {'continued': True},
    {'error_text': 123},
    {'trigger': 'unknown'},
    {'message_id': 'missing-assistant'},
])
def test_codec_rejects_invalid_activity_snapshots(openai_provider_config, tmp_path, changes):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        activity = controller.begin_compaction('manual')
        snapshot = controller._snapshot()
        snapshot['state']['compaction_activities'][activity.id].update(changes)
        with pytest.raises(SessionRepositoryError):
            SessionCodec.decode(snapshot, 'session')
    finally:
        controller.close()
