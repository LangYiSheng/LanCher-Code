from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from uuid import uuid4

import pytest

from lancher_code.execution.contracts import CommandProfile, ExecutionConfig, ProcessInfo
from lancher_code.execution.runtime import ExecutionRuntime
from lancher_code.errors import ConfigError
from lancher_code.models import StreamEvent, ToolCall, ToolCallChunk, ToolDefinition, ToolPermissionMetadata
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.turn_runner import TurnRunner
from lancher_code.sessions.repository import SessionRepositoryError


def python_command(script: str) -> str:
    if os.name == 'nt':
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        return f'& {quote(sys.executable)} -u -c {quote(script)}'
    return f'{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}'


class IdleProvider:
    async def stream_chat(self, request):
        yield StreamEvent(kind='text_delta', text='完成')
        yield StreamEvent(kind='message_end')


def setup_runtime(tmp_path, provider_config, provider=None):
    runtime = ExecutionRuntime(tmp_path, ExecutionConfig(command_profiles=[CommandProfile('测试脚本', '*')]))
    session = SessionController(provider_config, cwd=tmp_path, initial_permission_policy='bypass')
    registry = create_default_tool_registry()
    executor = ToolExecutor(registry, cwd=tmp_path, execution_runtime=runtime)
    runner = TurnRunner(provider or IdleProvider(), session, registry, executor)
    return runtime, session, executor, runner


async def launch(executor, session, script, *, lifetime='session', turn_id='origin'):
    arguments = {'description': '生命周期测试', 'command': python_command(script), 'yield_ms': 0, 'lifetime': lifetime}
    results = await executor.execute_calls([ToolCall(0, 'provider-call', 'run_command', arguments, json.dumps(arguments))],
        session_id=session.session_id, session_workspace=session.paths.workspace,
        session_root=session.paths.root, permission_policy='bypass', turn_id=turn_id)
    assert results[0].ok, results[0].error_message
    return results[0].metadata['process_id']


@pytest.mark.asyncio
async def test_switch_keeps_original_writer_and_background_event_owner(tmp_path, openai_provider_config):
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('第一段对话')
    original = session.session_id
    try:
        process_id = await launch(executor, session, "import time; print('source'); time.sleep(2)")
        original_service = session._sessions
        runner.new_session()
        session.create_user_message('另一段对话')
        other = session.session_id
        info = await runtime.processes.wait(process_id, original, timeout_ms=10000)
        assert info.status == 'exited'
        assert session.state.execution['processes'] == {}
        with pytest.raises(ValueError):
            runner.read_process_output(process_id, session_id=other)
        assert 'source' in runner.read_process_output(process_id, session_id=original)['text']
        runner.resume_session(original)
        assert original_service.writer is None  # 无后台的非当前会话主动释放 writer。
        assert session._sessions is not original_service
        assert session.state.execution['processes'][process_id]['status'] == 'exited'
        assert len(session.state.execution['inbox']) == 1
        # 退出只生成独立执行事件，原调用只有一条 tool_result。
        assert sum(block.kind == 'tool_result' for item in session.transcript for block in item.blocks) == 0
        session.create_user_message('查看此前任务')
        assert session.state.execution['inbox'] == []
        assert '状态通知' in session.transcript[-1].blocks[-1].text
        events = session._sessions.repository.read(original)
        assert sum(event['type'] == 'process.exited' for event in events) == 1
        assert not any(event['type'].startswith('process.') for event in session._sessions.repository.read(other))
    finally:
        await runner.shutdown()
        session.close()


class CommandsThenWait:
    def __init__(self):
        self.requests = 0
        self.waiting = asyncio.Event()

    async def stream_chat(self, request):
        self.requests += 1
        if self.requests == 1:
            for index, lifetime in enumerate(('turn', 'session')):
                arguments = {'description': lifetime, 'command': python_command(
                    f"import time; print('{lifetime}'); time.sleep(30)"), 'yield_ms': 0, 'lifetime': lifetime}
                yield StreamEvent(kind='tool_call_delta', tool_call_chunk=ToolCallChunk(
                    call_index=index, provider_call_id=f'call-{index}', name_delta='run_command',
                    arguments_delta=json.dumps(arguments)))
            yield StreamEvent(kind='message_end')
        else:
            self.waiting.set()
            await asyncio.Event().wait()
            yield StreamEvent(kind='message_end')


@pytest.mark.asyncio
async def test_stop_turn_kills_foreground_keeps_background_and_closes_all_on_exit(tmp_path, openai_provider_config):
    provider = CommandsThenWait()
    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config, provider)
    events = []

    async def consume():
        async for event in runner.run_user_turn('启动两个任务'):
            events.append(event)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(provider.waiting.wait(), 15)
        assert len(runner.list_processes()) == 2
        runner.cancel_active_turn()
        await asyncio.wait_for(consumer, 15)
        snapshots = {info['lifetime']: info for info in runner.list_processes()}
        assert snapshots['turn']['status'] == 'cancelled'
        assert snapshots['session']['status'] == 'running'
        assert any(event.kind == 'turn_cancelled' for event in events)
        assert len([event for event in events if event.kind == 'tool_result_received']) == 2
        await runner.stop_session()
        assert not runtime.processes.active_session(session.session_id)
        assert provider.requests == 2  # 停止后的退出事件不唤醒模型。
    finally:
        await runner.shutdown()
        await asyncio.gather(consumer, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
async def test_restart_marks_unknown_execution_lost_without_replaying(tmp_path, openai_provider_config):
    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('保存任务状态')
    owner, process_id = session.session_id, uuid4().hex
    info = ProcessInfo(process_id, owner, 'old-turn', uuid4().hex, 'do-not-run', '旧任务',
                       str(tmp_path), 'pipe', 'session', status='running', pid=999999)
    runtime.record_event(owner, 'process.started', info.to_dict())
    await runner.shutdown()
    session.close()
    replacement, restored, _, next_runner = setup_runtime(tmp_path, openai_provider_config)
    try:
        next_runner.resume_session(owner)
        saved = restored.state.execution['processes'][process_id]
        assert saved['status'] == 'lost'
        assert saved['exit_reason'] == 'application_restarted'
        assert not replacement.processes.active_session(owner)
        assert len(restored.state.execution['inbox']) == 1
        # 本次进程里从未创建过真实系统句柄。
        assert replacement.processes._processes == {}
    finally:
        await next_runner.shutdown()
        restored.close()


@pytest.mark.asyncio
async def test_archive_requires_stopping_inactive_session_processes(tmp_path, openai_provider_config):
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('后台服务')
    owner = session.session_id
    try:
        await launch(executor, session, 'import time; time.sleep(30)')
        runner.new_session()
        with pytest.raises(Exception, match='运行中的进程'):
            session.archive_session(owner)
        await runner.stop_session(owner)
        session.archive_session(owner)
        assert next(item for item in session.list_sessions() if item.session_id == owner).archived
    finally:
        await runner.shutdown()
        session.close()


@pytest.mark.asyncio
async def test_failed_retained_resume_keeps_current_session_and_provider(tmp_path, openai_provider_config, monkeypatch):
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('保留的源会话')
    original = session.session_id
    try:
        await launch(executor, session, 'import time; time.sleep(30)')
        runner.new_session()
        session.create_user_message('当前会话')
        current, writer, provider = session.session_id, session._sessions.writer, runner._provider
        binding = runtime.sessions.get(original)

        def fail(*args, **kwargs):
            raise SessionRepositoryError('模拟目标落盘失败')

        monkeypatch.setattr(binding.service, 'persist', fail)
        with pytest.raises(SessionRepositoryError, match='目标落盘失败'):
            runner.resume_session(original)
        assert session.session_id == current
        assert session._sessions.writer is writer
        assert runner._provider is provider
        session.create_user_message('失败后仍可正常保存')
        assert any(event['type'] == 'message.created' and event['data']['content'] == '失败后仍可正常保存'
                   for event in session._sessions.repository.read(current))
    finally:
        await runner.shutdown()
        session.close()


@pytest.mark.asyncio
async def test_failed_detach_checkpoint_keeps_writer_and_closes_target_lock(tmp_path, openai_provider_config, monkeypatch):
    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('磁盘中的目标')
    original = session.session_id
    runner.new_session()
    session.create_user_message('当前会话')
    current = session.session_id
    current_service = session._sessions
    real_checkpoint = current_service.checkpoint

    def fail():
        raise SessionRepositoryError('模拟checkpoint失败')

    try:
        monkeypatch.setattr(current_service, 'checkpoint', fail)
        with pytest.raises(SessionRepositoryError, match='checkpoint失败'):
            runner.resume_session(original)
        assert session.session_id == current
        assert current_service.writer is not None
        assert runtime.sessions.get(current).service is current_service
        # 异常 traceback 仍存在时，目标锁也已关闭。
        with current_service.repository.open(original):
            pass
        session.create_user_message('继续持久化')
        monkeypatch.setattr(current_service, 'checkpoint', real_checkpoint)
    finally:
        monkeypatch.setattr(current_service, 'checkpoint', real_checkpoint)
        await runner.shutdown()
        session.close()


@pytest.mark.asyncio
async def test_user_input_observer_cancellation_does_not_cancel_submitted_control(tmp_path):
    runtime = ExecutionRuntime(tmp_path)
    began, finish = asyncio.Event(), asyncio.Event()
    received = []

    async def submitted():
        began.set()
        await finish.wait()
        received.append('用户已提交的输入')

    observer = asyncio.create_task(runtime.run_control(submitted()))
    await began.wait()
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert runtime._control_tasks
    finish.set()
    await asyncio.sleep(0)
    await runtime.close()
    assert received == ['用户已提交的输入']
    assert not runtime._control_tasks


@pytest.mark.asyncio
async def test_concurrent_shutdown_waits_for_shared_cleanup(tmp_path, monkeypatch):
    runtime = ExecutionRuntime(tmp_path)
    began, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def close_processes():
        calls.append('close')
        began.set()
        await finish.wait()

    monkeypatch.setattr(runtime.processes, 'close', close_processes)
    first = asyncio.create_task(runtime.close())
    await began.wait()
    second = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    first.cancel()  # 关闭调用者取消，也要先完成已确定的系统资源回收。
    finish.set()
    await asyncio.gather(first, second)
    assert calls == ['close']


@pytest.mark.asyncio
async def test_cancelled_remote_side_effect_is_unknown_and_not_replayed(tmp_path, openai_provider_config):
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config)
    started = asyncio.Event()
    side_effects = []

    class RemoteWriter:
        definition = ToolDefinition('mcp__demo__submit', '测试远程提交', category='command',
            permission=ToolPermissionMetadata('external', 'mcp__demo__submit', 'MCP提交', 'demo', 'submit'))

        async def execute(self, arguments, context):
            side_effects.append('远端可能已经写入')
            started.set()
            await asyncio.Event().wait()

    executor._registry.register(RemoteWriter())

    class Provider:
        requests = 0

        async def stream_chat(self, request):
            self.requests += 1
            yield StreamEvent(kind='tool_call_delta', tool_call_chunk=ToolCallChunk(
                call_index=0, provider_call_id='remote-call', name_delta='mcp__demo__submit', arguments_delta='{}'))
            yield StreamEvent(kind='message_end')

    provider = Provider()
    runner._provider = provider

    async def consume():
        return [event async for event in runner.run_user_turn('提交远端任务')]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 5)
        runner.cancel_active_turn()
        await asyncio.wait_for(consumer, 5)
        invocation = runner.list_execution_tasks()[0]
        assert invocation['state'] == 'interrupted'
        assert invocation['error_code'] == 'mcp_outcome_unknown'
        results = [block for message in session.transcript for block in message.blocks if block.kind == 'tool_result']
        assert len(results) == 1 and '远端操作结果未知' in results[0].text
        assert provider.requests == 1 and len(side_effects) == 1
    finally:
        await runner.shutdown()
        await asyncio.gather(consumer, return_exceptions=True)
        session.close()


@pytest.mark.asyncio
async def test_session_stop_blocks_new_turn_and_file_calls_until_all_stops_finish(
    tmp_path, openai_provider_config, monkeypatch,
):
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('准备停止会话')
    began = asyncio.Queue()
    requested = 0
    finishes = [asyncio.Event(), asyncio.Event()]

    async def stop_processes(owner):
        nonlocal requested
        index = requested
        requested += 1
        # 两个停止请求各自停在实际进程收尾窗口。
        await began.put(index)
        await finishes[index].wait()

    monkeypatch.setattr(runtime.processes, 'stop_session', stop_processes)
    stops = []
    try:
        stops.append(asyncio.create_task(runner.stop_session()))
        assert await asyncio.wait_for(began.get(), 1) == 0
        stops.append(asyncio.create_task(runner.stop_session()))
        assert await asyncio.wait_for(began.get(), 1) == 1
        assert not runner.has_active_turn
        finishes[0].set()
        await stops[0]
        # 第一次停止完成不能提前打开第二次仍在收尾的入口。
        with pytest.raises(ConfigError, match='会话正在停止'):
            _ = [event async for event in runner.run_user_turn('不能提前开始')]
        args = {'path': 'new.txt', 'content': '不应写入'}
        result, = await executor.execute_calls(
            [ToolCall(0, 'late-file', 'write_file', args, json.dumps(args))],
            session_id=session.session_id, generation=runtime.generation(session.session_id),
            permission_policy='bypass',
        )
        assert result.error_code == 'steering_superseded'
        assert not (tmp_path / 'new.txt').exists()
        finishes[1].set()
        await stops[1]
        assert runtime.accepting(session.session_id)
        events = [event async for event in runner.run_user_turn('收尾后继续')]
        assert any(event.kind == 'turn_completed' for event in events)
    finally:
        for event in finishes:
            event.set()
        await asyncio.gather(*stops, return_exceptions=True)
        await runner.shutdown()
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cached_metadata', ['stale', 'corrupt', 'missing'])
async def test_recovered_process_queries_use_journal_over_derived_cache(
    tmp_path, openai_provider_config, cached_metadata,
):
    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config)
    session.create_user_message('保留已完成的进程事实')
    owner, process_id = session.session_id, uuid4().hex
    info = ProcessInfo(process_id, owner, 'old-turn', uuid4().hex, 'do-not-run', '已完成任务',
                       str(tmp_path), 'pipe', 'session', status='exited', exit_code=0,
                       exit_reason='completed')
    runtime.record_event(owner, 'process.exited', info.to_dict())
    directory = session.paths.root / 'processes' / process_id
    if cached_metadata != 'missing':
        directory.mkdir(parents=True)
        content = ('{' if cached_metadata == 'corrupt' else json.dumps(
            dict(info.to_dict(), status='running', exit_code=None, exit_reason=None)))
        (directory / 'meta.json').write_text(content, encoding='utf-8')
    await runner.shutdown()
    session.close()
    replacement, restored, _, next_runner = setup_runtime(tmp_path, openai_provider_config)
    try:
        next_runner.resume_session(owner)
        records = next_runner.list_processes()
        assert len(records) == 1 and records[0]['process_id'] == process_id
        assert records[0]['status'] == 'exited' and records[0]['exit_reason'] == 'completed'
        assert replacement.processes.get(process_id, owner).exit_code == 0
        assert not replacement.processes.active_session(owner)
        assert replacement.processes._processes == {}  # 只恢复事实，没有创建系统句柄。
    finally:
        await next_runner.shutdown()
        restored.close()
