from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
from uuid import uuid4

import pytest

from lancher_code.execution.contracts import CommandProfile, ExecutionConfig, ExecutionLimits, ProcessInfo, ResourceClaim
from lancher_code.execution.runtime import ExecutionRuntime
from lancher_code.execution.scheduler import get_project_scheduler
from lancher_code.errors import ConfigError
from lancher_code.models import PermissionResolution, StreamEvent, ToolCall, ToolCallChunk, ToolDefinition, ToolPermissionMetadata
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


def setup_runtime(tmp_path, provider_config, provider=None, *, execution_config=None):
    config = execution_config or ExecutionConfig(command_profiles=[CommandProfile('测试脚本', '*')])
    runtime = ExecutionRuntime(tmp_path, config)
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
@pytest.mark.parametrize('eager', [False, True])
async def test_successive_turns_own_distinct_scopes_before_execution(
    tmp_path, openai_provider_config, eager,
):
    """真实 TUI 的立即调度不能让首轮完成封住后续轮次。"""
    class Provider:
        requests = 0

        async def stream_chat(self, request):
            self.requests += 1
            if self.requests == 2:
                arguments = {'description': '检查 Python', 'command': python_command("print('scope-ok')"),
                             'yield_ms': 10000}
                yield StreamEvent(kind='tool_call_delta', tool_call_chunk=ToolCallChunk(
                    call_index=0, provider_call_id='scope-call', name_delta='run_command',
                    arguments_delta=json.dumps(arguments)))
            else:
                yield StreamEvent(kind='text_delta', text='完成')
            yield StreamEvent(kind='message_end')

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config, Provider())
    try:
        if eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        first = [event async for event in runner.run_user_turn('先聊天')]
        second = [event async for event in runner.run_user_turn('再检查 Python')]
        result, = [event.tool_result for event in second if event.kind == 'tool_result_received']
        assert result.ok, result.error_message
        assert 'scope-ok' in result.content
        first_id, = [event.task_id for event in first if event.kind == 'turn_completed']
        second_id, = [event.task_id for event in second if event.kind == 'turn_completed']
        assert first_id and second_id and first_id != second_id
        journal = session._sessions.repository.read(session.session_id)
        starts = [event['turn_id'] for event in journal if event['type'] == 'turn.started']
        finishes = [event['turn_id'] for event in journal if event['type'] == 'turn.completed']
        assert starts == finishes == [first_id, second_id]
        assert result.metadata['origin_turn_id'] == second_id
        assert (session.session_id, None) not in runtime.processes._stopping_turns
        assert not runner.has_active_turn
    finally:
        await runner.shutdown()
        session.close()
        loop.set_task_factory(previous_factory)


@pytest.mark.asyncio
async def test_stop_before_bound_turn_starts_closes_event_consumer(tmp_path, openai_provider_config):
    """立即调度创建消费者后、轮次启动门闩恢复前的停止仍能完整收尾。"""
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config)
    consumer = None

    async def consume():
        return [event async for event in runner.run_user_turn('立即停止')]

    try:
        loop.set_task_factory(asyncio.eager_task_factory)
        consumer = asyncio.create_task(consume())
        assert runner.cancel_active_turn()
        await asyncio.wait_for(consumer, 2)
        assert not runner.has_active_turn
        assert not runtime.processes._processes
        # 停止入口未开始的轮次，不封住下一次正常请求。
        events = [event async for event in runner.run_user_turn('现在继续')]
        assert any(event.kind == 'turn_completed' and event.task_id for event in events)
    finally:
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await runner.shutdown()
        session.close()
        loop.set_task_factory(previous_factory)


@pytest.mark.asyncio
async def test_stop_during_completed_turn_cleanup_keeps_terminal_journal(tmp_path, openai_provider_config):
    class Provider:
        requests = 0

        async def stream_chat(self, request):
            self.requests += 1
            if self.requests == 1:
                arguments = {'description': '本轮托管进程', 'command': python_command('import time; time.sleep(30)'),
                             'yield_ms': 0}
                yield StreamEvent(kind='tool_call_delta', tool_call_chunk=ToolCallChunk(
                    call_index=0, provider_call_id='cleanup-call', name_delta='run_command',
                    arguments_delta=json.dumps(arguments)))
            else:
                yield StreamEvent(kind='text_delta', text='完成')
            yield StreamEvent(kind='message_end')

    runtime, session, _, runner = setup_runtime(tmp_path, openai_provider_config, Provider())
    completed_id = None

    async def consume():
        nonlocal completed_id
        async for event in runner.run_user_turn('启动本轮任务后结束回答'):
            if event.kind == 'turn_completed':
                completed_id = event.task_id
                # 回答已完成，托管进程仍在 finally 中进行真实停止。
                assert runner.cancel_active_turn()

    try:
        await asyncio.wait_for(consume(), 15)
        assert completed_id and not runner.has_active_turn
        assert not runtime.processes.active_session(session.session_id)
        journal = session._sessions.repository.read(session.session_id)
        assert [event['turn_id'] for event in journal if event['type'] == 'turn.completed'] == [completed_id]
        assert not any(event['type'] == 'turn.interrupted' for event in journal)
    finally:
        await runner.shutdown()
        session.close()


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


@pytest.mark.parametrize('client', ['python', 'curl'])
@pytest.mark.asyncio
async def test_unknown_background_http_server_allows_approved_clients_and_file_tools(
    tmp_path, openai_provider_config, client,
):
    """未知后台 Shell 只串行启动调用；返回句柄后，已批准的请求和文件工具仍能工作。"""
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if client == 'curl' and curl is None:
        pytest.skip('当前平台没有安装 curl，Python 客户端场景仍覆盖真实 HTTP 请求。')
    config = ExecutionConfig(limits=ExecutionLimits(stop_grace_seconds=.05, drain_timeout_seconds=1))
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config, execution_config=config)
    session.create_user_message('后台服务启动后继续检查页面')
    (tmp_path / 'index.html').write_text('HTTP-RESOURCE-SCOPE', encoding='utf-8')
    approved_commands = []

    async def approve(request):
        approved_commands.append(request.command)
        return PermissionResolution(request.request_id, 'allow_once')

    async def execute(name, arguments):
        result, = await executor.execute_calls(
            [ToolCall(0, uuid4().hex, name, arguments, json.dumps(arguments))],
            session_id=session.session_id, session_workspace=session.paths.workspace,
            session_root=session.paths.root, turn_id='http-check', permission_policy='default',
            permission_resolver=approve,
        )
        assert result.ok, result.error_message
        return result

    process_id = None
    try:
        script = ("from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler;"
                  "server=ThreadingHTTPServer(('127.0.0.1',0),SimpleHTTPRequestHandler);"
                  "print('SERVER-PORT='+str(server.server_port),flush=True);server.serve_forever()")
        server = await asyncio.wait_for(execute('run_command', {
            'description': '启动本地 HTTP 服务', 'command': python_command(script),
            'yield_ms': 0, 'lifetime': 'session',
        }), 5)
        process_id = server.metadata['process_id']
        assert server.metadata['status'] == 'running'
        async with asyncio.timeout(5):
            while True:
                lines = runtime.processes.read(process_id, session.session_id).text.splitlines()
                ports = [line.removeprefix('SERVER-PORT=') for line in lines if line.startswith('SERVER-PORT=')]
                if ports:
                    port = int(ports[0])
                    break
                await asyncio.sleep(.01)
        url = f'http://127.0.0.1:{port}/'
        if client == 'python':
            client_command = python_command(
                f"import urllib.request;print(urllib.request.urlopen({url!r},timeout=3).read().decode())")
        elif os.name == 'nt':
            client_command = f"& '{curl.replace(chr(39), chr(39)*2)}' --silent --show-error --fail --max-time 3 {url}"
        else:
            client_command = f'{shlex.quote(curl)} --silent --show-error --fail --max-time 3 {url}'
        response = await asyncio.wait_for(execute('run_command', {
            'description': '已批准的 HTTP 客户端', 'command': client_command, 'yield_ms': 5000,
        }), 7)
        assert client_command in approved_commands
        assert response.metadata['exit_code'] == 0 and 'HTTP-RESOURCE-SCOPE' in response.content
        assert runtime.processes.get(process_id, session.session_id).status == 'running'
        read = await asyncio.wait_for(execute('read_file', {'path': 'index.html'}), 2)
        assert 'HTTP-RESOURCE-SCOPE' in read.content
        await asyncio.wait_for(execute('write_file', {'path': 'client-check.txt', 'content': '文件工具仍可写入'}), 2)
        assert (tmp_path / 'client-check.txt').read_text(encoding='utf-8') == '文件工具仍可写入'
        assert runtime.processes.get(process_id, session.session_id).status == 'running'
        await runtime.processes.stop(process_id, session.session_id)
        assert not runtime.processes.active_session(session.session_id)
        assert get_project_scheduler(tmp_path).active_count == 0
    finally:
        await runner.shutdown()
        session.close()


@pytest.mark.parametrize('resource_kind', ['project', 'path'])
@pytest.mark.asyncio
async def test_explicit_process_profile_blocks_until_real_exit_and_reports_process_owner(
    tmp_path, openai_provider_config, resource_kind,
):
    """显式资源约定持续到真实退出；等待信息必须指向后台进程，而非已完成的调用。"""
    held = tmp_path / 'held'
    held.mkdir()
    server_command = python_command("print('PROFILE-READY',flush=True);input();print('PROFILE-EXIT')")
    key = str(tmp_path) if resource_kind == 'project' else 'held'
    claim = ResourceClaim(resource_kind, key, recursive=True)
    config = ExecutionConfig(limits=ExecutionLimits(stop_grace_seconds=.05, drain_timeout_seconds=1),
        command_profiles=[CommandProfile('显式进程资源', server_command, resources=(claim,))])
    runtime, session, executor, runner = setup_runtime(tmp_path, openai_provider_config, execution_config=config)
    session.create_user_message('等待已声明资源的后台进程退出')
    waiting = None
    process_id = None

    async def execute(name, arguments, *, call_id=None, on_started=None):
        result, = await executor.execute_calls(
            [ToolCall(0, call_id or uuid4().hex, name, arguments, json.dumps(arguments))],
            session_id=session.session_id, session_workspace=session.paths.workspace,
            session_root=session.paths.root, turn_id='profile-turn', permission_policy='bypass',
            on_call_started=on_started,
        )
        assert result.ok, result.error_message
        return result

    async def after_real_cleanup(call):
        assert runtime.processes._processes[process_id].done.is_set()
        assert runtime.processes.get(process_id, session.session_id).exit_code == 0

    try:
        server = await asyncio.wait_for(execute('run_command', {
            'description': '持有已声明资源', 'command': server_command, 'yield_ms': 0, 'lifetime': 'session',
        }), 5)
        process_id = server.metadata['process_id']
        waiter_id = uuid4().hex
        waiting = asyncio.create_task(execute('write_file', {'path': 'held/result.txt', 'content': '退出后写入'},
            call_id=waiter_id, on_started=after_real_cleanup))
        async with asyncio.timeout(5):
            while True:
                invocation = next((item for item in runtime.list_invocations(session.session_id)
                                   if item['provider_call_id'] == waiter_id), None)
                if invocation and invocation['state'] == 'waiting_resources' and invocation['waiting'].get('blockers'):
                    break
                await asyncio.sleep(.01)
        blocker, = invocation['waiting']['blockers']
        assert invocation['waiting']['reason'] == 'resource_conflict'
        assert blocker['process_id'] == process_id
        assert blocker['invocation_id'] == server.metadata['origin_invocation_id']
        assert blocker['session_id'] == session.session_id and blocker['tool_name'] == 'run_command'
        assert blocker['resources'][0]['kind'] == resource_kind
        assert blocker['resources'][0]['lifetime'] == 'process'
        assert not waiting.done() and not (held / 'result.txt').exists()
        if resource_kind == 'path':
            await asyncio.wait_for(execute('write_file', {'path': 'independent.txt', 'content': '独立路径可写'}), 2)
            assert not waiting.done()
        assert runtime.processes.get(process_id, session.session_id).status == 'running'
        # process_write 只需要 stdin 管理锁，能让持有项目资源的服务自行结束。
        await asyncio.wait_for(execute('process_write', {'process_id': process_id, 'text': '\n'}), 3)
        ended = await runtime.processes.wait(process_id, session.session_id, timeout_ms=5000)
        assert ended.status == 'exited' and ended.exit_code == 0
        await asyncio.wait_for(waiting, 5)
        assert (held / 'result.txt').read_text(encoding='utf-8') == '退出后写入'
        completed = next(item for item in runtime.list_invocations(session.session_id) if item['provider_call_id'] == waiter_id)
        assert completed['state'] == 'succeeded' and completed['waiting'] == {}
        assert not runtime.processes.active_session(session.session_id)
        assert get_project_scheduler(tmp_path).active_count == 0
    finally:
        await runner.shutdown()
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        session.close()


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
