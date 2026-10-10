from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest
from provider_helpers import complete_test_response

from lancher_code.context.prefix import validate_prefix_state, validate_prefix_transcript
from lancher_code.context.summary import SUMMARY_HEADINGS
from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.messages import ContentBlock, ConversationMessage, StreamEvent
from lancher_code.contracts.tools import ToolDefinition, ToolExecutionResult
from lancher_code.errors import ContextCompactionError
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.repository import ProjectSessionRepository, SessionRepositoryError


START = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)


@pytest.fixture
def session(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller._request_clock = lambda: START
    try:
        yield controller
    finally:
        controller.close()


def tool(name, *, description='读取数据'):
    return ToolDefinition(name=name, description=description, input_schema={'type': 'object'})


def test_changes_append_without_rewriting_previous_system_or_messages(session):
    blocks = ['<project_instructions>旧约定</project_instructions>', '可用 Skills：\n- 检查']
    session.bind_agent_context(lambda state: list(blocks))
    session.create_user_message('先查看')
    first = session.build_request([tool('read')], allow_tool_calls=True)
    epoch = session.context_state.prefix_state['epoch']
    blocks[0] = '<project_instructions>新约定</project_instructions>'
    blocks.append('<active_skill id="project/check">技能正文</active_skill>')
    session.set_work_phase('plan')
    session.set_permission_policy('acceptEdits')
    session.create_user_message('计划一下')
    second = session.build_request([tool('read'), tool('mcp_data')], allow_tool_calls=True)
    assert second.system == first.system
    assert second.messages[:len(first.messages)] == first.messages
    assert session.context_state.prefix_state['epoch'] == epoch
    assert session.transcript[0].blocks == [ContentBlock.text_block('先查看')]
    latest = second.messages[-1].blocks[0].text
    assert '新约定' in latest and '技能正文' in latest
    assert '用户刚进入 Plan Mode' in latest and 'acceptEdits' in latest
    assert list(session.context_state.prefix_state['observed_tools']) == ['read', 'mcp_data']


def test_preview_does_not_commit_events_or_change_frozen_state(session):
    session.create_user_message('问题')
    session.build_request([], allow_tool_calls=True)
    session.set_work_phase('discuss')
    session.bind_agent_context(lambda state: ['<active_skill id="project/demo">待加载正文</active_skill>'])
    before = copy.deepcopy(session.context_state)
    history = copy.deepcopy(session.transcript)
    saved = session.paths.events.read_bytes()
    preview = session.preview_request([tool('new')], allow_tool_calls=True)
    assert '待加载正文' in preview.messages[-1].blocks[0].text
    assert session.context_state == before
    assert session.transcript == history
    assert session.paths.events.read_bytes() == saved


def test_cancellation_and_resume_preserve_every_sent_message(session, openai_provider_config, tmp_path):
    session.create_user_message('开始')
    request = session.build_request([tool('read')], allow_tool_calls=True)
    assistant = session.create_assistant_message()
    session.cancel_message(assistant.id, '停止')
    assert session.transcript == request.messages
    saved_id = session.session_id
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored._request_clock = lambda: START
    try:
        restored.resume_session(saved_id, resolved_model=(openai_provider_config, None))
        resumed = restored.build_request([tool('read')], allow_tool_calls=True)
        assert resumed.system == request.system
        assert resumed.messages == request.messages
        assert restored.context_state.prefix_state['epoch'] == session.context_state.prefix_state['epoch']
    finally:
        restored.close()


@pytest.mark.parametrize('replacement', [
    '<skill_reference id="project/demo">需要时重新加载</skill_reference>',
    '<skill_unavailable>project/demo 目录已删除</skill_unavailable>',
    '',
])
def test_removing_skill_body_reclaims_old_event_and_rebuilds_epoch(session, replacement):
    blocks = ['可用 Skills：demo', '<active_skill id="project/demo">secret-body-123</active_skill>']
    session.bind_agent_context(lambda state: list(blocks))
    session.create_user_message('执行')
    first = session.build_request([], allow_tool_calls=True)
    original_epoch = session.context_state.prefix_state['epoch']
    assert 'secret-body-123' in repr(first.messages)
    blocks[:] = ['可用 Skills：'] + ([replacement] if replacement else [])
    rebuilt = session.build_request([], allow_tool_calls=True)
    assert session.context_state.prefix_state['epoch'] != original_epoch
    assert 'secret-body-123' not in repr(rebuilt)
    assert 'secret-body-123' not in repr(session.context_state.prefix_state)
    assert 'secret-body-123' not in repr(session.transcript)
    assert session.transcript[0].blocks[0].text == '执行'


def test_time_updates_only_after_idle_threshold_or_date_change(session):
    session.create_user_message('继续')
    first = session.build_request([], allow_tool_calls=True)
    session._request_clock = lambda: START + timedelta(minutes=15)
    assert session.build_request([], allow_tool_calls=True).messages == first.messages
    session._request_clock = lambda: START + timedelta(minutes=46)
    idle = session.build_request([], allow_tool_calls=True)
    assert len(idle.messages) == len(first.messages) + 1
    assert 'host_time' in idle.messages[-1].blocks[0].text
    assert session.build_request([], allow_tool_calls=True).messages == idle.messages
    session._request_clock = lambda: START + timedelta(days=1)
    next_day = session.build_request([], allow_tool_calls=True)
    assert next_day.system == first.system
    assert '2026-10-11' in next_day.messages[-1].blocks[0].text
    assert next_day.messages[:len(idle.messages)] == idle.messages


def test_prefix_event_commit_is_atomic_and_failed_write_rolls_back(session, monkeypatch):
    session.create_user_message('已有任务')
    first = session.build_request([], allow_tool_calls=True)
    previous = copy.deepcopy(session.context_state)
    original_history = copy.deepcopy(session.transcript)
    original_append = session._sessions.writer.append

    def fail_prefix(kind, *args, **kwargs):
        if kind == 'context.prefix_updated':
            raise SessionRepositoryError('模拟增量提交失败')
        return original_append(kind, *args, **kwargs)

    session.bind_agent_context(lambda state: ['<active_skill id="project/new">未提交正文</active_skill>'])
    with monkeypatch.context() as patch:
        patch.setattr(session._sessions.writer, 'append', fail_prefix)
        with pytest.raises(SessionRepositoryError, match='增量提交失败'):
            session.build_request([], allow_tool_calls=True)
    assert session.context_state == previous
    assert session.transcript == original_history
    events = ProjectSessionRepository(session.project_root).read(session.session_id)
    state, transcript, _, _ = SessionCodec.decode(SessionCodec.project(events), session.session_id)
    assert transcript == first.messages
    assert state.context_management == previous
    committed = session.build_request([], allow_tool_calls=True)
    events = ProjectSessionRepository(session.project_root).read(session.session_id)
    assert events[-1]['type'] == 'context.prefix_updated'
    assert events[-1]['data']['replace'] is False
    state, transcript, _, _ = SessionCodec.decode(SessionCodec.project(events), session.session_id)
    assert transcript == committed.messages
    assert state.context_management.prefix_state == session.context_state.prefix_state


def test_post_commit_runtime_failure_keeps_durable_prefix(session, monkeypatch):
    session.create_user_message('任务')
    session.build_request([], allow_tool_calls=True)
    session.set_work_phase('discuss')
    with monkeypatch.context() as patch:
        patch.setattr(session, '_register_execution_session', lambda: (_ for _ in ()).throw(RuntimeError('挂接失败')))
        with pytest.raises(RuntimeError, match='挂接失败'):
            session.build_request([], allow_tool_calls=True)
    events = ProjectSessionRepository(session.project_root).read(session.session_id)
    state, transcript, _, _ = SessionCodec.decode(SessionCodec.project(events), session.session_id)
    assert transcript == session.transcript
    assert state.context_management == session.context_state
    assert state.context_management.prefix_state['observed']['phase'] == 'discuss'


def test_failed_freeze_commit_rolls_back_even_when_prefix_itself_is_unchanged(session, monkeypatch):
    session.create_user_message('任务')
    session.build_request([], allow_tool_calls=True)
    session.append_assistant_response([ContentBlock.tool_use_block(call_id='read-1', name='read', input={})])
    session.append_tool_results([ToolExecutionResult('read-1', 'read', content='刚读取的结果')])
    previous = copy.deepcopy(session.context_state)
    history = copy.deepcopy(session.transcript)
    original_append = session._sessions.writer.append

    def fail_prefix(kind, *args, **kwargs):
        if kind == 'context.prefix_updated':
            raise SessionRepositoryError('冻结预览提交失败')
        return original_append(kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(session._sessions.writer, 'append', fail_prefix)
        with pytest.raises(SessionRepositoryError, match='冻结预览提交失败'):
            session.build_request([], allow_tool_calls=True)
    assert session.context_state == previous
    assert session.transcript == history
    assert session.context_state.frozen_tool_previews == {}


def test_native_tools_use_baseline_and_persist_final_message_anchors(session):
    session.configure_prompt_experiments(True)
    session.create_user_message('查询')
    first = session.build_request([tool('builtin')], allow_tool_calls=True)
    # 混合跨协议工具结果会拆成两条投影消息，raw 下标不能直接发送。
    session._transcript.extend([
        ConversationMessage(role='assistant', response_protocol='claude', response_model='old', blocks=[
            ContentBlock.tool_use_block(call_id='old', name='read', input={})]),
        ConversationMessage(role='assistant', response_protocol='openai', response_model=session.provider_config.model, blocks=[
            ContentBlock.tool_use_block(call_id='current', name='read', input={})]),
        ConversationMessage(role='tool', blocks=[ContentBlock.tool_result_block(call_id='old', text='旧', is_error=False),
                                                ContentBlock.tool_result_block(call_id='current', text='新', is_error=False)]),
    ])
    second = session.build_request([tool('builtin'), tool('mcp_new')], allow_tool_calls=True)
    assert second.messages[:len(first.messages)] == first.messages
    assert [item.name for item in second.tools] == ['builtin']
    update = second.tool_updates[0]
    assert update['at_message'] == len(second.messages) - 1
    assert update['at_message'] > session.context_state.prefix_state['tool_events'][0]['anchor']
    assert second.messages[update['at_message']].blocks[0].text.startswith('<host_update ')
    assert [item['name'] for item in update['additions']] == ['mcp_new']
    assert update['removals'] == []
    epoch = session.context_state.prefix_state['epoch']
    disabled = session.build_request([tool('builtin'), tool('mcp_new')], allow_tool_calls=False)
    assert disabled.tools == second.tools and disabled.tool_updates == second.tool_updates
    assert disabled.messages == second.messages
    assert session.context_state.prefix_state['epoch'] == epoch


@pytest.mark.parametrize('change', ['remove', 'schema'])
def test_openai_native_removal_or_schema_change_rebuilds_baseline(session, change):
    session.configure_prompt_experiments(True)
    session.create_user_message('任务')
    session.build_request([tool('read'), tool('extra')], allow_tool_calls=True)
    old_epoch = session.context_state.prefix_state['epoch']
    current = [tool('read')] if change == 'remove' else [tool('read'), tool('extra', description='新定义')]
    rebuilt = session.build_request(current, allow_tool_calls=True)
    assert rebuilt.tool_updates == []
    assert rebuilt.tools == current
    assert session.context_state.prefix_state['epoch'] != old_epoch


def test_host_only_tool_metadata_does_not_rebuild_native_cache(session):
    session.configure_prompt_experiments(True)
    session.create_user_message('任务')
    definition = tool('read')
    first = session.build_request([definition], allow_tool_calls=True)
    epoch = session.context_state.prefix_state['epoch']
    definition.allowed_phases = ('discuss', 'plan', 'execute')
    definition.is_system_tool = True
    second = session.build_request([definition], allow_tool_calls=True)
    assert second.system == first.system and second.messages == first.messages
    assert second.tool_updates == []
    assert session.context_state.prefix_state['epoch'] == epoch
    assert session.context_state.prefix_state['observed_tools']['read']['is_system_tool'] is True


def test_claude_native_removal_is_tail_event(claude_provider_config, tmp_path):
    controller = SessionController(claude_provider_config, cwd=tmp_path)
    controller._request_clock = lambda: START
    try:
        controller.configure_prompt_experiments(True)
        controller.create_user_message('任务')
        first = controller.build_request([tool('read'), tool('extra')], allow_tool_calls=True)
        epoch = controller.context_state.prefix_state['epoch']
        second = controller.build_request([tool('read')], allow_tool_calls=True)
        assert second.tools == first.tools
        assert second.messages[:len(first.messages)] == first.messages
        assert second.tool_updates[-1]['removals'] == ['extra']
        assert controller.context_state.prefix_state['epoch'] == epoch
    finally:
        controller.close()


def test_native_recovery_with_same_config_does_not_rebuild(session, openai_provider_config, tmp_path):
    session.configure_prompt_experiments(True)
    session.create_user_message('任务')
    session.build_request([tool('read')], allow_tool_calls=True)
    last = session.build_request([tool('read'), tool('mcp')], allow_tool_calls=True)
    saved_id = session.session_id
    epoch = session.context_state.prefix_state['epoch']
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored._request_clock = lambda: START
    restored.configure_prompt_experiments(True)
    try:
        restored.resume_session(saved_id)
        next_request = restored.build_request([tool('read'), tool('mcp')], allow_tool_calls=True)
        assert next_request.system == last.system
        assert next_request.messages == last.messages
        assert next_request.tools == last.tools and next_request.tool_updates == last.tool_updates
        assert restored.context_state.prefix_state['epoch'] == epoch
        restored.configure_prompt_experiments(False)
        normal = restored.build_request([tool('read'), tool('mcp')], allow_tool_calls=True)
        assert [item.name for item in normal.tools] == ['read', 'mcp']
        assert not normal.experimental_mcp_tool_append and normal.tool_updates == []
        assert restored.context_state.prefix_state['epoch'] != epoch
    finally:
        restored.close()


def test_interrupted_tool_recovery_reanchors_persisted_native_events(session, openai_provider_config, tmp_path):
    session.configure_prompt_experiments(True)
    session.create_user_message('查询')
    session.build_request([tool('read')], allow_tool_calls=True)
    session.append_assistant_response([ContentBlock.tool_use_block(call_id='pending', name='read', input={})])
    session.set_work_phase('discuss')
    session.build_request([tool('read'), tool('mcp_new')], allow_tool_calls=True)
    event = copy.deepcopy(session.context_state.prefix_state['events'][-1])
    saved_id = session.session_id
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored._request_clock = lambda: START
    restored.configure_prompt_experiments(True)
    try:
        restored.resume_session(saved_id)
        prefix = restored.context_state.prefix_state
        assert prefix['events'][-1]['text'] == event['text']
        assert prefix['events'][-1]['anchor'] == event['anchor'] + 1
        assert prefix['tool_events'][-1]['anchor'] == event['anchor'] + 1
        validate_prefix_transcript(prefix, restored.transcript)
        assert restored.transcript[event['anchor']].blocks[0].kind == 'tool_result'
        assert restored.transcript[event['anchor']].blocks[0].is_error is True
        events = ProjectSessionRepository(tmp_path).read(saved_id)
        state, transcript, _, _ = SessionCodec.decode(SessionCodec.project(events), saved_id)
        assert transcript == restored.transcript and state.context_management.prefix_state == prefix
        request = restored.build_request([tool('read'), tool('mcp_new')], allow_tool_calls=True)
        assert request.messages[request.tool_updates[-1]['at_message']].blocks[0].text == event['text']
    finally:
        restored.close()


@pytest.mark.parametrize('field,value', [('seq', True), ('seq', 2), ('anchor', -1), ('anchor', True)])
def test_invalid_event_sequence_or_anchor_is_rejected(session, field, value):
    session.create_user_message('任务')
    session.build_request([], allow_tool_calls=True)
    prefix = copy.deepcopy(session.context_state.prefix_state)
    prefix['events'][0][field] = value
    with pytest.raises(ValueError):
        validate_prefix_state(prefix)


def test_event_text_must_match_persisted_transcript_and_old_sessions_are_compatible(session):
    session.create_user_message('任务')
    session.build_request([], allow_tool_calls=True)
    prefix = copy.deepcopy(session.context_state.prefix_state)
    prefix['events'][0]['text'] = '伪造事件'
    with pytest.raises(ValueError, match='不一致'):
        validate_prefix_transcript(prefix, session.transcript)
    snapshot = session._snapshot()
    snapshot['state']['context_management'].pop('prefix_state')
    snapshot['state']['context_management'].pop('frozen_tool_previews')
    state, _, _, _ = SessionCodec.decode(snapshot, session.session_id)
    assert state.context_management.prefix_state == {}
    assert state.context_management.frozen_tool_previews == {}


class SummaryProvider:
    def __init__(self, *, invalid=False):
        self.requests = []
        self.invalid = invalid

    @complete_test_response
    async def stream_chat(self, request):
        self.requests.append(request)
        summary = '<summary>' + '\n'.join(f'## {heading}\n已整理历史。' for heading in SUMMARY_HEADINGS) + '</summary>'
        yield StreamEvent(kind='text_delta', text='无效摘要' if self.invalid else summary)
        yield StreamEvent(kind='message_end', response_complete=True)


def skill_history(session):
    state = session.context_state
    state.skill_activations['project/demo'] = dict(
        id='project/demo', name='demo', description='检查', scope='project', path='SKILL.md', directory='.',
        digest='a' * 64, body='secret-body-123', activation_kind='explicit', loaded=True)
    session.bind_agent_context(lambda state: [
        '<active_skill id="project/demo">secret-body-123</active_skill>' if state.skill_activations['project/demo']['loaded']
        else '<skill_reference id="project/demo">引用保留，需要时重新加载</skill_reference>'])
    for index in range(3):
        session.create_user_message(f'旧材料{index}' + 'x' * 16000)
        reply = session.create_assistant_message()
        session.append_message_content(reply.id, '已记录。')
        session.complete_message(reply.id)
    session.create_user_message('继续当前任务')
    session.build_request([], allow_tool_calls=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('experimental', [False, True])
async def test_successful_compaction_removes_host_bodies_and_retains_skill_reference(session, experimental):
    session.configure_prompt_experiments(experimental)
    skill_history(session)
    provider = SummaryProvider()
    old_epoch = session.context_state.prefix_state['epoch']
    await session.compact_context(provider=provider, visible_tools=[])
    assert session.context_state.prefix_state == {}
    assert session.context_state.frozen_tool_previews == {}
    assert session.context_state.skill_activations['project/demo']['body'] == ''
    assert session.context_state.skill_activations['project/demo']['loaded'] is False
    assert 'secret-body-123' not in repr(session.transcript)
    assert all('secret-body-123' not in repr(request.messages) for request in provider.requests)
    assert all(request.experimental_mcp_tool_append is experimental for request in provider.requests)
    next_request = session.build_request([], allow_tool_calls=True)
    assert '<skill_reference id="project/demo">' in repr(next_request)
    assert session.context_state.prefix_state['epoch'] != old_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['cancel', 'invalid'])
async def test_unsuccessful_compaction_keeps_exact_prefix_and_skill_body(session, failure):
    skill_history(session)
    state = copy.deepcopy(session.context_state)
    history = copy.deepcopy(session.transcript)
    token = CancellationToken()
    if failure == 'cancel':
        token.cancel()
    expected = asyncio.CancelledError if failure == 'cancel' else ContextCompactionError
    with pytest.raises(expected):
        await session.compact_context(provider=SummaryProvider(invalid=True), visible_tools=[], cancellation_token=token)
    assert session.context_state == state
    assert session.transcript == history
