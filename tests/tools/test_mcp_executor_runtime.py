from __future__ import annotations

import asyncio

import pytest
from mcp import types

from lancher_code.contracts.tools import ToolCall
from lancher_code.mcp.adapter import MCPToolAdapter
from lancher_code.mcp.config import MCPServerConfig
from lancher_code.permissions.models import PermissionResolution
from lancher_code.sessions.controller import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry


class Connection:
    def __init__(self):
        self.config = MCPServerConfig(name="demo", type="stdio", command="python", tool_timeout_seconds=60)
        self.calls = 0

    async def call_tool(self, name, arguments):
        self.calls += 1
        await asyncio.sleep(0.03)
        return types.CallToolResult(content=[types.TextContent(type="text", text="done")])


def adapter(connection, *, read_only):
    return MCPToolAdapter("demo", types.Tool(name="lookup", inputSchema={"type": "object"},
                                           annotations=types.ToolAnnotations(readOnlyHint=read_only)), connection)


def call():
    return ToolCall(call_index=0, call_id="test", tool_name="mcp__demo__lookup", arguments={}, arguments_json="{}")


@pytest.mark.asyncio
@pytest.mark.parametrize('removed', [False, True])
async def test_tool_changed_during_model_response_cannot_execute_new_binding(tmp_path, removed):
    old_connection, new_connection = Connection(), Connection()
    old_tool, new_tool = adapter(old_connection, read_only=False), adapter(new_connection, read_only=False)
    registry = ToolRegistry()
    registry.register_deferred_server('demo', title='Demo', description=None)
    registry.register(old_tool, deferred_server_name='demo')
    expected = {old_tool.definition.name: old_tool}
    # 同一个参数 {} 对两个版本都合法，schema 校验自身无法发现版本失效。
    registry.replace_deferred_server('demo', [] if removed else [new_tool], title='Demo', description=None)
    approvals, starts = [], []
    async def resolver(request):
        approvals.append(request)
        return PermissionResolution(request.request_id, 'allow_once')
    async def started(call):
        starts.append(call)
    executor = ToolExecutor(registry, cwd=tmp_path)
    result = (await executor.execute_calls([call()], expected_tool_bindings=expected,
        available_tool_names=set(expected), permission_resolver=resolver, on_call_started=started))[0]
    assert result.error_code == 'tool_changed' and result.metadata['outcome'] == 'not_started'
    assert approvals == starts == [] and old_connection.calls == new_connection.calls == 0


@pytest.mark.asyncio
async def test_matching_request_binding_executes_and_hidden_tool_still_requires_search(tmp_path):
    connection = Connection()
    tool = adapter(connection, read_only=True)
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, cwd=tmp_path)
    result = (await executor.execute_calls([call()], expected_tool_bindings={tool.definition.name: tool},
                                        available_tool_names={tool.definition.name}))[0]
    assert not result.is_error and connection.calls == 1
    result = (await executor.execute_calls([call()], expected_tool_bindings={}, available_tool_names=set()))[0]
    assert result.error_code == 'tool_not_found' and result.metadata['requires_tool_search'] is True
    assert connection.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'timeout'])
@pytest.mark.parametrize('catalog_change', ['remove', 'replace_with_read'])
async def test_running_remote_write_keeps_unknown_outcome_after_catalog_change(
    tmp_path, monkeypatch, ending, catalog_change,
):
    class GatedConnection(Connection):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
        async def call_tool(self, name, arguments):
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()

    connection = GatedConnection()
    tool = adapter(connection, read_only=False)
    registry = ToolRegistry()
    registry.register_deferred_server('demo', title='Demo', description=None)
    registry.register(tool, deferred_server_name='demo')
    executor = ToolExecutor(registry, cwd=tmp_path)
    original = executor._await_cancelable
    async def shorter_tool_deadline(awaitable, context, *, timeout=None):
        # 测执行器兜底超时；adapter 自身的 60 秒期限不能先把异常转换成结果。
        return await original(awaitable, context, timeout=0.05 if timeout is not None else None)
    if ending == 'timeout':
        monkeypatch.setattr(executor, '_await_cancelable', shorter_tool_deadline)
    task = asyncio.create_task(executor.execute_calls([call()], permission_policy='bypass',
        expected_tool_bindings={tool.definition.name: tool}))
    try:
        await asyncio.wait_for(connection.started.wait(), 2)
        if catalog_change == 'remove':
            registry.unregister_deferred_server('demo')
        else:
            registry.replace_deferred_server('demo', [adapter(Connection(), read_only=True)], title='Demo', description=None)
        if ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = (await asyncio.wait_for(task, 2))[0]
            assert result.error_code == 'mcp_outcome_unknown'
            assert result.metadata['automatic_retry'] is False
        record = executor.execution_runtime.list_invocations(None)[0]
        assert record['state'] == 'interrupted' and record['error_code'] == 'mcp_outcome_unknown'
        assert connection.calls == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_executor_honors_mcp_timeout_instead_of_short_default(tmp_path, monkeypatch):
    connection = Connection()
    registry = ToolRegistry()
    registry.register(adapter(connection, read_only=True))
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=0.01)
    budgets = []
    original = executor._await_cancelable

    async def observe(awaitable, context, *, timeout=None):
        if timeout is not None:
            budgets.append(timeout)
        return await original(awaitable, context, timeout=timeout)

    monkeypatch.setattr(executor, "_await_cancelable", observe)
    result = (await executor.execute_calls([call()]))[0]
    assert not result.is_error and result.content == "done"
    assert connection.calls == 1
    assert budgets == [61.0]


@pytest.mark.asyncio
@pytest.mark.parametrize('after_started_notification', [False, True])
async def test_catalog_replacement_while_awaiting_approval_rejects_stale_tool(
    tmp_path, openai_provider_config, after_started_notification,
):
    old_connection, new_connection = Connection(), Connection()
    registry = ToolRegistry()
    registry.register_deferred_server("demo", title="Demo", description=None)
    registry.register(adapter(old_connection, read_only=False), deferred_server_name="demo")
    waiting, approve = asyncio.Event(), asyncio.Event()
    starts = []

    async def on_started(call):
        starts.append(call.call_id)
        if after_started_notification:
            registry.replace_deferred_server('demo', [adapter(new_connection, read_only=False)], title='Demo', description=None)

    async def resolver(request):
        waiting.set()
        await approve.wait()
        return PermissionResolution(request.request_id, "allow_once")

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [call()], permission_resolver=resolver, on_call_started=on_started))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        if not after_started_notification:
            registry.replace_deferred_server("demo", [adapter(new_connection, read_only=False)], title="Demo", description=None)
        approve.set()
        result = (await asyncio.wait_for(task, 1))[0]
        assert result.error_code == "tool_changed"
        assert result.metadata['started'] is False
        assert starts == (['test'] if after_started_notification else [])
        assert old_connection.calls == new_connection.calls == 0
        session = SessionController(openai_provider_config, cwd=tmp_path)
        try:
            session.create_user_message('验证工具状态')
            assistant = session.create_assistant_message()
            session.append_trace_tool_calls(assistant.id, [call()])
            if after_started_notification:
                session.set_trace_tool_state(assistant.id, 'test', 'running')
            session.append_trace_tool_results(assistant.id, [result])
            assert all(entry.metadata['started'] is False for entry in assistant.trace.entries)
        finally:
            session.close()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
