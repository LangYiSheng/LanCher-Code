from __future__ import annotations

import asyncio
import dataclasses
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from lancher_code.errors import ToolNotFoundError
from lancher_code.mcp.adapter import MCPToolAdapter
from lancher_code.mcp.config import MCPServerConfig, validate_server_config
from lancher_code.mcp.connection import MCPServerConnection, MCPServerDiscovery
from lancher_code.mcp.manager import MCPClientManager
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.registry import ToolRegistry


def remote_tool(name="lookup", *, output_schema=None, read_only=True):
    return types.Tool(name=name, inputSchema={"type": "object"}, outputSchema=output_schema,
                      annotations=types.ToolAnnotations(readOnlyHint=read_only))


@pytest.mark.parametrize("field", ["startup_timeout_seconds", "tool_timeout_seconds", "close_timeout_seconds"])
@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), float("nan"), "60", None])
def test_timeout_validation_rejects_non_finite_or_non_positive_values(field, value):
    with pytest.raises(ValueError):
        validate_server_config("demo", {"type": "stdio", "command": "python", field: value})


class RuntimeConnection:
    def __init__(self, config):
        self.config = config
        self.name = config.name
        self.tools = [remote_tool("old")]
        self.closed = False
        self.failed_refresh = False
        self.changed_callbacks = []
        self.disconnect_callbacks = []

    def add_tools_changed_callback(self, callback):
        self.changed_callbacks.append(callback)

    def add_disconnect_callback(self, callback):
        self.disconnect_callbacks.append(callback)

    async def connect_and_list_tools(self):
        return await self.refresh_tools()

    async def refresh_tools(self):
        if self.failed_refresh:
            raise ConnectionError("failed")
        return MCPServerDiscovery(types.Implementation(name=self.name, version="1"), tuple(self.tools),
                                  types.ServerCapabilities(tools=types.ToolsCapability(listChanged=True)))

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_refresh_and_reconnect_replace_catalog_and_shutdown_removes_tools():
    created = []

    def factory(config):
        connection = RuntimeConnection(config)
        created.append(connection)
        return connection

    manager = MCPClientManager([MCPServerConfig(name="demo", type="stdio", command="python")], connection_factory=factory)
    registry = ToolRegistry()
    progress = []
    manager.add_progress_callback(progress.append)
    await manager.initialize(registry)
    snapshot = manager.status()[0]
    assert snapshot.capabilities == ("tools", "tools.listChanged")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.state = "tampered"
    created[0].tools = [remote_tool("new")]
    await manager.refresh(registry, "demo")
    assert registry.get("mcp__demo__new")
    with pytest.raises(ToolNotFoundError):
        registry.get("mcp__demo__old")
    assert snapshot.state == "ready" and snapshot.registered_tools == 1
    await manager.reconnect(registry, "demo")
    assert created[0].closed
    assert registry.get("mcp__demo__old")
    with pytest.raises(ToolNotFoundError):
        registry.get("mcp__demo__new")
    await manager.shutdown(registry)
    assert created[1].closed and manager.status()[0].state == "stopped"
    assert registry.list_deferred_index() == []


@pytest.mark.asyncio
async def test_refresh_failure_and_disconnect_invalidate_catalog_without_auto_reconnect():
    manager = MCPClientManager([MCPServerConfig(name="demo", type="stdio", command="python")], connection_factory=RuntimeConnection)
    registry = ToolRegistry()
    progress = []
    manager.add_progress_callback(progress.append)
    await manager.initialize(registry)
    connection = manager._connections["demo"]
    connection.failed_refresh = True
    await manager.refresh(registry, "demo")
    assert manager.status()[0].state == "failed"
    assert (progress[-1].successful_servers, progress[-1].failed_servers) == (0, 1)
    assert registry.search_deferred("old") == []
    connection.failed_refresh = False
    await manager.refresh(registry, "demo")
    for callback in connection.disconnect_callbacks:
        callback()
    assert manager.status()[0].state == "disconnected"
    assert (progress[-1].successful_servers, progress[-1].failed_servers) == (0, 1)
    assert registry.search_deferred("old") == []
    assert not connection.closed
    await manager.shutdown()


@pytest.mark.asyncio
async def test_list_changed_notification_refreshes_in_core():
    manager = MCPClientManager([MCPServerConfig(name="demo", type="stdio", command="python")], connection_factory=RuntimeConnection)
    registry = ToolRegistry()
    await manager.initialize(registry)
    connection = manager._connections["demo"]
    connection.tools = [remote_tool("new")]
    for callback in connection.changed_callbacks:
        callback()
    async with asyncio.timeout(1):
        while manager._refresh_tasks:
            await asyncio.sleep(0)
    assert registry.get("mcp__demo__new")
    assert registry.search_deferred("old") == []
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_refresh_keeps_previous_catalog_and_can_refresh_again():
    class GatedConnection(RuntimeConnection):
        def __init__(self, config):
            super().__init__(config)
            self.started = asyncio.Event()
            self.gate = None

        async def refresh_tools(self):
            if self.gate is not None:
                self.started.set()
                await self.gate.wait()
            return await super().refresh_tools()

    manager = MCPClientManager([MCPServerConfig(name="demo", type="stdio", command="python")], connection_factory=GatedConnection)
    registry = ToolRegistry()
    await manager.initialize(registry)
    connection = manager._connections["demo"]
    original = registry.get("mcp__demo__old")
    connection.tools = [remote_tool("new")]
    connection.gate = asyncio.Event()
    task = asyncio.create_task(manager.refresh(registry, "demo"))
    try:
        await asyncio.wait_for(connection.started.wait(), 1)
        assert manager.status()[0].state == "refreshing"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.status()[0].state == "ready" and manager.status()[0].registered_tools == 1
        assert registry.get("mcp__demo__old") is original
        assert not registry.search_deferred("select:mcp__demo__new")
        connection.gate.set()
        await manager.refresh(registry, "demo")
        assert registry.get("mcp__demo__new")
        assert manager.status()[0].state == "ready"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during", ["close", "connect"])
async def test_cancelled_reconnect_clears_catalog_and_can_reconnect_again(cancel_during):
    started, gate = asyncio.Event(), asyncio.Event()
    created = []

    class GatedConnection(RuntimeConnection):
        async def connect_and_list_tools(self):
            if len(created) == 2 and cancel_during == "connect":
                started.set()
                await gate.wait()
            return await super().connect_and_list_tools()

        async def close(self):
            if self is created[0] and cancel_during == "close":
                started.set()
                await gate.wait()
            await super().close()

    def factory(config):
        connection = GatedConnection(config)
        created.append(connection)
        return connection

    manager = MCPClientManager([MCPServerConfig(name="demo", type="stdio", command="python")], connection_factory=factory)
    registry = ToolRegistry()
    await manager.initialize(registry)
    task = asyncio.create_task(manager.reconnect(registry, "demo"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.status()[0].state == "disconnected"
        assert manager.status()[0].registered_tools == 0 and not registry.list_deferred_index()
        assert not manager._connections
        gate.set()
        await manager.reconnect(registry, "demo")
        assert manager.status()[0].state == "ready" and registry.get("mcp__demo__old")
    finally:
        gate.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_connection_reads_all_pages_and_rejects_repeated_cursor():
    class Session:
        def __init__(self):
            self.cursors = []
            self.repeat = False

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            self.cursors.append(cursor)
            return types.ListToolsResult(tools=[remote_tool("first" if cursor is None else "last")],
                                         nextCursor="next" if cursor is None or self.repeat else None)

    connection = MCPServerConnection(MCPServerConfig(name="demo", type="stdio", command="python"))
    session = Session()
    connection._session = session
    assert [tool.name for tool in await connection.list_tools()] == ["first", "last"]
    assert session.cursors == [None, "next"]
    session.repeat = True
    with pytest.raises(RuntimeError, match="游标重复"):
        await connection.list_tools()


@pytest.mark.asyncio
async def test_pagination_supports_older_sdk_cursor_signature():
    class LegacySession:
        async def list_tools(self, cursor=None):
            return types.ListToolsResult(tools=[remote_tool("first" if cursor is None else "last")],
                                         nextCursor="next" if cursor is None else None)

    connection = MCPServerConnection(MCPServerConfig(name="demo", type="stdio", command="python"))
    connection._session = LegacySession()
    assert [tool.name for tool in await connection.list_tools()] == ["first", "last"]


@pytest.mark.asyncio
async def test_adapter_preserves_resources_and_structured_content_and_validates_output(tmp_path):
    class Connection:
        async def call_tool(self, name, arguments):
            return types.CallToolResult(content=[
                types.TextContent(type="text", text="ok"),
                types.ResourceLink(type="resource_link", name="doc", title="文档", uri="https://example.test/doc"),
                types.EmbeddedResource(type="resource", resource=types.TextResourceContents(uri="memo://one", text="资料原文")),
                types.AudioContent(type="audio", data="private-binary", mimeType="audio/wav"),
            ], structuredContent={"value": 2})

    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    result = await MCPToolAdapter("demo", remote_tool(output_schema=schema), Connection()).execute({}, context)
    assert not result.is_error and result.metadata["output_schema_valid"] is True
    assert result.metadata["structured_content"] == {"value": 2}
    assert len(result.metadata["resources"]) == 2
    assert "资料原文" in result.content and "https://example.test/doc" in result.content
    assert "private-binary" not in result.content
    schema["properties"]["value"]["type"] = "string"
    invalid = await MCPToolAdapter("demo", remote_tool(output_schema=schema, read_only=False), Connection()).execute({}, context)
    assert invalid.error_code == "mcp_output_invalid"
    assert invalid.metadata["automatic_retry"] is False
    assert not invalid.metadata.get("outcome_unknown", False)


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', ['https://example.invalid/schema.json', 'file:///private/schema.json'])
async def test_output_schema_validation_does_not_retrieve_external_references(tmp_path, monkeypatch, reference):
    import urllib.request

    def unexpected_retrieve(*args, **kwargs):
        raise AssertionError('输出校验不能访问外部 Schema')

    monkeypatch.setattr(urllib.request, 'urlopen', unexpected_retrieve)

    class Connection:
        calls = 0

        async def call_tool(self, name, arguments):
            self.calls += 1
            return types.CallToolResult(content=[], structuredContent={'value': 2})

    connection = Connection()
    result = await MCPToolAdapter('demo', remote_tool(output_schema={'$ref': reference}), connection).execute(
        {}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    assert result.error_code == 'mcp_output_invalid'
    assert result.metadata['output_schema_valid'] is False
    assert result.metadata['automatic_retry'] is False and connection.calls == 1


@pytest.mark.asyncio
async def test_output_schema_supports_local_definitions(tmp_path):
    class Connection:
        async def call_tool(self, name, arguments):
            return types.CallToolResult(content=[], structuredContent={'value': 2})

    schema = {'$defs': {'result': {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                                'required': ['value']}}, '$ref': '#/$defs/result'}
    result = await MCPToolAdapter('demo', remote_tool(output_schema=schema), Connection()).execute(
        {}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    assert not result.is_error and result.metadata['output_schema_valid'] is True


@pytest.mark.asyncio
async def test_remote_protocol_error_is_known_and_call_timeout_is_unknown_for_writes(tmp_path):
    class ProtocolErrorConnection:
        async def call_tool(self, name, arguments):
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="invalid"))

    class SlowConnection:
        config = MCPServerConfig(name="demo", type="stdio", command="python", tool_timeout_seconds=0.01)

        async def call_tool(self, name, arguments):
            await asyncio.sleep(1)

    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    remote = remote_tool(read_only=False)
    protocol = await MCPToolAdapter("demo", remote, ProtocolErrorConnection()).execute({}, context)
    timeout = await MCPToolAdapter("demo", remote, SlowConnection()).execute({}, context)
    assert protocol.error_code == "mcp_remote_error"
    assert timeout.error_code == "mcp_outcome_unknown"
    assert timeout.metadata["automatic_retry"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_real_transports_pagination_notifications_slow_call_and_shutdown(transport, tmp_path):
    server_path = Path(__file__).with_name("tools_runtime_test_server.py")
    process = None
    if transport == "stdio":
        config = MCPServerConfig(name="live", type="stdio", command=sys.executable, args=[str(server_path)], tool_timeout_seconds=1)
    else:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(server_path), str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        async with asyncio.timeout(10):
            while True:
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    assert process.poll() is None, "HTTP 测试服务启动失败"
                    await asyncio.sleep(0.02)
        config = MCPServerConfig(name="live", type="http", url=f"http://127.0.0.1:{port}/mcp/", tool_timeout_seconds=1)
    manager = MCPClientManager([config])
    registry = ToolRegistry()
    try:
        await manager.initialize(registry)
        assert manager.status()[0].state == "ready"
        assert manager.status()[0].registered_tools == 4
        assert "tools.listChanged" in manager.status()[0].capabilities
        context = ToolContext(cwd=tmp_path, timeout_seconds=0.01)
        slow = await registry.get("mcp__live__slow").execute({}, context)
        assert not slow.is_error
        changed = await registry.get("mcp__live__mutate").execute({}, context)
        assert not changed.is_error
        async with asyncio.timeout(5):
            while not registry.search_deferred("select:mcp__live__new"):
                await asyncio.sleep(0.01)
        assert not registry.search_deferred("select:mcp__live__old")
        if transport == "stdio":
            await registry.get("mcp__live__echo").execute({"exit_after": True}, context)
            async with asyncio.timeout(5):
                while manager.status()[0].state != "disconnected":
                    await asyncio.sleep(0.01)
            assert registry.list_deferred_index() == []
    finally:
        await manager.shutdown(registry)
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
    assert manager.status()[0].state == "stopped"
    assert registry.list_deferred_index() == []
