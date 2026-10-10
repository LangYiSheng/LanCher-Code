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
