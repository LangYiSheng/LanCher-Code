from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
from provider_helpers import complete_test_response

from lancher_code.agent.runner import TurnRunner
from lancher_code.agent.skills import SkillError, SkillsService
from lancher_code.contracts.messages import StreamEvent
from lancher_code.contracts.tools import DeferredToolGroup, ToolCallChunk
from lancher_code.context.prompts import build_deferred_tools_prompt
from lancher_code.errors import ContextCompactionError, ProviderRequestError
from lancher_code.mcp.manager import MCPServerStatus
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry


BODY = 'SKILL_BODY_SENTINEL：先读取现有设计，再执行任务。'


def skill(root, *, explicit_only=False, body=BODY):
    directory = root / '.lancher' / 'skills' / 'review'
    directory.mkdir(parents=True, exist_ok=True)
    extra = 'disable-model-invocation: true\n' if explicit_only else ''
    (directory / 'SKILL.md').write_text(
        f'---\nname: review\ndescription: 审核代码变更\n{extra}---\n{body}\n', encoding='utf-8')


def final(text='完成'):
    return [StreamEvent(kind='text_delta', text=text), StreamEvent(kind='message_end', response_complete=True)]


def load():
    return [StreamEvent(kind='tool_call_delta', tool_call_chunk=ToolCallChunk(
        call_index=0, provider_call_id='load-1', name_delta='load_skill', arguments_delta='{"skill":"review"}')),
        StreamEvent(kind='message_end', response_complete=True)]


def summary():
    headings = ('主要请求和意图', '关键技术概念', '文件和代码段', '错误与修复', '问题解决过程',
                '用户消息与明确反馈', '待办任务', '当前工作', '可能的下一步')
    return final('<summary>' + '\n'.join(f'## {heading}\n已记录' for heading in headings) + '</summary>')


class Provider:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.entered = asyncio.Event()
        self.gate = None

    @complete_test_response
    async def stream_chat(self, request):
        self.requests.append(request)
        self.entered.set()
        response = self.responses.pop(0)
        if self.gate is not None and len(self.requests) == 1:
            await self.gate.wait()
        if response is None:
            await asyncio.Event().wait()
        elif isinstance(response, BaseException):
            raise response
        else:
            for event in response:
                yield event


def make_runner(root, config, responses, *, manager=None):
    service = SkillsService(root, user_root=root / 'isolated-home')
    session = SessionController(config, cwd=root)
    registry = ToolRegistry()
    provider = Provider(responses)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=root),
                        skills_service=service, mcp_manager=manager)
    return runner, session, provider


async def close(runner, session):
    await runner.shutdown()
    session.close()


def history(session):
    return '\n'.join(block.text for message in session.transcript for block in message.blocks)


def add_history(session):
    session.create_user_message('旧任务：' + 'x' * 50_000)
    old = session.create_assistant_message()
    session.append_message_content(old.id, '此前已完成')
    session.complete_message(old.id)
    session.create_user_message('继续处理')


async def test_explicit_skill_persists_across_turns_and_session_restore_without_tui(tmp_path, openai_provider_config):
    skill(tmp_path)
    (tmp_path / 'AGENTS.md').write_text('PROJECT_RULE_SENTINEL：中文注释', encoding='utf-8')
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [final(), final()])
    try:
        assert (await collect(runner, '$review 检查代码'))[-1].kind == 'turn_completed'
        assert (await collect(runner, '继续'))[-1].kind == 'turn_completed'
        assert all(BODY in '\n'.join(request.system) for request in provider.requests)
        assert all('PROJECT_RULE_SENTINEL' in '\n'.join(request.system) for request in provider.requests)
        assert BODY not in history(session)
        session_id = session.session_id
    finally:
        await close(runner, session)
    restored, session, provider = make_runner(tmp_path, openai_provider_config, [final()])
    try:
        restored.resume_session(session_id)
        assert session.context_state.skill_activations['project/review']['loaded']
        await collect(restored, '继续之前的审核')
        assert BODY in '\n'.join(provider.requests[0].system)
    finally:
        await close(restored, session)


async def collect(runner, text):
    return [event async for event in runner.run_user_turn(text)]


async def test_model_load_adds_body_only_to_next_system_request(tmp_path, openai_provider_config):
    skill(tmp_path)
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [load(), final()])
    try:
        assert (await collect(runner, '帮我审核代码'))[-1].kind == 'turn_completed'
        assert BODY not in '\n'.join(provider.requests[0].system)
        assert BODY in '\n'.join(provider.requests[1].system)
        assert BODY not in history(session)
        assert '已加载技能 project/review' in history(session)
    finally:
        await close(runner, session)


async def test_successful_compaction_reclaims_body_and_reloads_explicit_only_reference(tmp_path, openai_provider_config):
    skill(tmp_path, explicit_only=True)
    (tmp_path / 'AGENTS.md').write_text('PROJECT_RULE_SENTINEL', encoding='utf-8')
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [summary(), load(), final()])
    try:
        add_history(session)
        runner.capabilities.skills.apply_explicit('$review')
        result = await runner.compact_context()
        assert result.after_tokens < result.before_tokens
        activation = session.context_state.skill_activations['project/review']
        assert activation['body'] == '' and not activation['loaded']
        request = session.build_request([], allow_tool_calls=True)
        assert BODY not in '\n'.join(request.system)
        assert '<skill_reference' in '\n'.join(request.system)
        assert 'PROJECT_RULE_SENTINEL' in '\n'.join(request.system)
        await collect(runner, '继续审核')
        assert BODY not in '\n'.join(provider.requests[-2].system)
        assert BODY in '\n'.join(provider.requests[-1].system)
        assert session.context_state.skill_activations['project/review']['activation_kind'] == 'explicit'
        assert BODY not in history(session)
    finally:
        await close(runner, session)


@pytest.mark.parametrize('failure', ['provider', 'persist'])
async def test_failed_compaction_preserves_skill_snapshot(tmp_path, openai_provider_config, monkeypatch, failure):
    skill(tmp_path)
    responses = [ProviderRequestError('summary failed')] if failure == 'provider' else [summary()]
    runner, session, _provider = make_runner(tmp_path, openai_provider_config, responses)
    try:
        add_history(session)
        runner.capabilities.skills.apply_explicit('$review')
        before = copy.deepcopy(session.context_state.skill_activations)
        if failure == 'persist':
            original = session.flush

            def reject(*args, **kwargs):
                if kwargs.get('context_event') == 'context.compacted':
                    raise SessionRepositoryError('模拟压缩提交失败')
                return original(*args, **kwargs)

            monkeypatch.setattr(session, 'flush', reject)
        with pytest.raises((ContextCompactionError, ProviderRequestError, SessionRepositoryError)):
            await runner.compact_context()
        assert session.context_state.skill_activations == before
        assert BODY in '\n'.join(session.build_request([], allow_tool_calls=True).system)
    finally:
        await close(runner, session)


async def test_cancelled_compaction_preserves_skill_snapshot(tmp_path, openai_provider_config):
    skill(tmp_path)
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [None])
    task = None
    try:
        add_history(session)
        runner.capabilities.skills.apply_explicit('$review')
        before = copy.deepcopy(session.context_state.skill_activations)
        task = asyncio.create_task(runner.compact_context())
        await asyncio.wait_for(provider.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.context_state.skill_activations == before
    finally:
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await close(runner, session)


async def test_explicit_only_disabled_and_new_session_isolation(tmp_path, openai_provider_config):
    skill(tmp_path, explicit_only=True)
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [load(), final()])
    try:
        await collect(runner, '帮我审核')
        assert BODY not in '\n'.join(provider.requests[-1].system)
        assert '需要用户通过 $review 显式指定' in history(session)
        runner.capabilities.skills.apply_explicit('$review')
        runner.capabilities.set_skill_enabled('review', False)
        assert BODY not in '\n'.join(session.build_request([], allow_tool_calls=True).system)
        with pytest.raises(SkillError, match='禁用'):
            runner.capabilities.skills.apply_explicit('$review')
        runner.new_session()
        assert session.context_state.skill_activations == {}
        assert runner.capabilities.list_skills()[0]['enabled']
    finally:
        await close(runner, session)


def test_large_deferred_index_has_bound_without_losing_search_catalog():
    groups = [DeferredToolGroup('demo', 'Demo', '服务说明', tuple(f'mcp__demo__tool_{n}' for n in range(1000))),
              DeferredToolGroup('other', 'Other', None, ('mcp__other__read',))]
    prompt = build_deferred_tools_prompt(groups, max_chars=700)
    assert len(prompt) <= 700
    assert '1001 个工具' in prompt and '<tool_count>1000</tool_count>' in prompt
    assert '<name>Other</name>' in prompt and 'tool_search' in prompt
    assert len(groups[0].tool_names) == 1000


class Manager:
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.initializations, self.closed = 0, False
        self.current = MCPServerStatus('demo', 'waiting', 'stdio')

    def add_progress_callback(self, callback):
        self.callback = callback

    def status(self):
        return (self.current,)

    async def initialize(self, registry):
        self.initializations += 1
        self.entered.set()
        await self.release.wait()
        self.current = replace(self.current, state='ready')

    async def refresh(self, registry, server_name):
        assert not self.closed
        self.current = replace(self.current, state='failed', last_error='目录刷新失败')
        return self.status()

    async def reconnect(self, registry, server_name):
        assert not self.closed
        self.current = replace(self.current, state='failed', last_error='连接失败')
        return self.status()

    async def close(self):
        self.closed = True

    async def shutdown(self, registry=None):
        await self.close()


async def test_mcp_initialization_owned_by_core_is_nonblocking_and_failure_visible(tmp_path, openai_provider_config):
    manager = Manager()
    runner, session, _provider = make_runner(tmp_path, openai_provider_config, [final()], manager=manager)
    try:
        runner.start_capabilities()
        runner.start_capabilities()
        await asyncio.wait_for(manager.entered.wait(), 5)
        assert (await asyncio.wait_for(collect(runner, '本地任务'), 5))[-1].kind == 'turn_completed'
        refresh = asyncio.create_task(runner.capabilities.refresh_mcp())
        await asyncio.sleep(0)
        assert not refresh.done() and not manager.closed
        manager.release.set()
        assert '目录刷新失败' in await refresh
        assert '重连失败' in await runner.capabilities.reconnect_mcp('demo')
        runner.start_capabilities()
        assert manager.initializations == 1
    finally:
        manager.release.set()
        await close(runner, session)
    assert manager.closed


async def test_reload_caller_cancel_does_not_close_core_initialization(tmp_path, openai_provider_config, monkeypatch):
    import lancher_code.agent.capabilities as capabilities_module
    previous, replacement = Manager(), Manager()
    runner, session, _provider = make_runner(tmp_path, openai_provider_config, [], manager=previous)
    monkeypatch.setattr(capabilities_module, 'load_mcp_config', lambda root: ([], []))
    monkeypatch.setattr(capabilities_module, 'MCPClientManager', lambda *args, **kwargs: replacement)
    task = None
    try:
        previous.release.set()
        runner.start_capabilities()
        await asyncio.wait_for(previous.entered.wait(), 5)
        task = asyncio.create_task(runner.capabilities.reload_mcp())
        await asyncio.wait_for(replacement.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert previous.closed and not replacement.closed
        replacement.release.set()
        await runner.capabilities.refresh_mcp()
        assert runner.capabilities.mcp_status()[0]['state'] == 'failed'
        assert replacement.initializations == 1 and not replacement.closed
    finally:
        replacement.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await close(runner, session)


async def test_loaded_snapshot_can_change_source_and_promote_explicit_authorization(tmp_path, openai_provider_config):
    skill(tmp_path)
    runner, session, _provider = make_runner(tmp_path, openai_provider_config, [])
    try:
        session.create_user_message('检查代码')
        runtime = runner.capabilities.skills
        runtime.activate(runtime.service.load('review'))
        assert session.context_state.skill_activations['project/review']['activation_kind'] == 'automatic'
        runtime.apply_explicit('$review')
        assert session.context_state.skill_activations['project/review']['activation_kind'] == 'explicit'
        # 注册目录来源改变时，已加载旧快照不能阻止重新激活新来源。
        replacement_root = tmp_path / 'replacement'
        skill(replacement_root, body='NEW_SOURCE_SENTINEL')
        runtime.service = SkillsService(replacement_root, user_root=tmp_path / 'isolated-home')
        assert '来源已改变' in '\n'.join(runtime.context_blocks(session.context_state))
        runtime.activate(runtime.service.load('review'))
        assert 'NEW_SOURCE_SENTINEL' in '\n'.join(runtime.context_blocks(session.context_state))
        assert BODY not in '\n'.join(runtime.context_blocks(session.context_state))
    finally:
        await close(runner, session)


@pytest.mark.parametrize('failure', ['disabled', 'missing', 'budget'])
async def test_failed_steering_skill_keeps_entire_input_batch_paused(tmp_path, openai_provider_config, failure):
    skill(tmp_path)
    directory = tmp_path / '.lancher' / 'skills' / 'blocked'
    directory.mkdir()
    body = 'x' * 24_000 if failure == 'budget' else '另一份指南'
    path = directory / 'SKILL.md'
    path.write_text(f'---\nname: blocked\ndescription: 错误边界测试\n---\n{body}', encoding='utf-8')
    openai_provider_config.context_window = 8192
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [final('已处理原任务')])
    provider.gate = asyncio.Event()
    task = None
    try:
        session.create_user_message('建立会话')
        if failure == 'disabled':
            runner.capabilities.set_skill_enabled('blocked', False)
        elif failure == 'missing':
            path.unlink()
        task = asyncio.create_task(collect(runner, '原任务'))
        await asyncio.wait_for(provider.entered.wait(), 5)
        first = runner.enqueue_input('$review 第一个补充', 'steer')
        second = runner.enqueue_input('$blocked 第二个补充', 'steer')
        third = runner.enqueue_input('第三个补充不能丢失', 'steer')
        provider.gate.set()
        events = await asyncio.wait_for(task, 10)
        terminal = [event for event in events if event.kind in {'turn_completed', 'turn_failed', 'turn_cancelled'}]
        assert terminal[-1].kind == 'turn_failed'
        assert [item.id for item in runner.pending_inputs] == [first.id, second.id, third.id]
        assert all(item.state == 'paused' for item in runner.pending_inputs)
        assert not any(event.kind == 'steering_applied' for event in events)
        assert session.context_state.skill_activations == {}
        assert not any(event.kind == 'assistant_message_completed' for event in events)
    finally:
        provider.gate.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await close(runner, session)


@pytest.mark.parametrize('invalid', ['utf8', 'oversize'])
def test_project_instructions_error_is_visible_and_recovers_after_file_fixed(tmp_path, invalid):
    from lancher_code.agent.instructions import project_instructions
    path = tmp_path / 'AGENTS.md'
    path.write_bytes(b'\xff' if invalid == 'utf8' else b'x' * 32_769)
    assert '<project_instructions_issue>' in project_instructions(tmp_path)
    path.write_text('UPDATED_RULE_SENTINEL </project_instructions>', encoding='utf-8')
    rendered = project_instructions(tmp_path)
    assert 'UPDATED_RULE_SENTINEL' in rendered
    assert '&lt;/project_instructions&gt;' in rendered


async def test_explicit_skill_failure_is_persisted_in_failed_assistant_message(tmp_path, openai_provider_config):
    skill(tmp_path)
    runner, session, provider = make_runner(tmp_path, openai_provider_config, [])
    try:
        runner.capabilities.set_skill_enabled('review', False)
        events = await collect(runner, '$review 检查代码')
        failed = next(event for event in events if event.kind == 'turn_failed')
        assert failed.message is not None and failed.message.status == 'error'
        assert '已被禁用' in failed.error_text
        assert session.state.messages[-1] is failed.message
        assert provider.requests == [] and not session.context_state.skill_activations
    finally:
        await close(runner, session)
