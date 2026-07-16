import asyncio
import sys
from pathlib import Path

import pytest
from mcp import types

from lancher_code.mcp.adapter import MCPToolAdapter
from lancher_code.mcp.config import MCPServerConfig
from lancher_code.mcp.manager import MCPClientManager
from lancher_code.mcp.connection import MCPServerConnection, MCPServerDiscovery
from lancher_code.models import DeferredToolGroup, ToolContext
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.logging_system import close_logging, configure_logging, register_sensitive_values


class FakeConnection:
    def __init__(
        self,
        config: MCPServerConfig,
        tools: list[types.Tool] | None = None,
        server_info: types.Implementation | None = None,
    ) -> None:
        self.name = config.name
        self.tools = tools or []
        self.server_info = server_info or types.Implementation(name=config.name, version="1.0.0")
        self.closed = False

    async def connect_and_list_tools(self) -> MCPServerDiscovery:
        await asyncio.sleep(0)
        return MCPServerDiscovery(self.server_info, tuple(self.tools))

    async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text="first"),
                types.ImageContent(type="image", data="secret-data", mimeType="image/png"),
                types.TextContent(type="text", text="last"),
            ]
        )

    async def close(self) -> None:
        self.closed = True


def remote_tool(name: str = "lookup", *, read_only: bool = True) -> types.Tool:
    return types.Tool(
        name=name,
        description="remote",
        inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
        annotations=types.ToolAnnotations(readOnlyHint=read_only),
    )


@pytest.mark.asyncio
async def test_adapter_maps_definition_and_safely_converts_content(tmp_path: Path) -> None:
    config = MCPServerConfig(name="demo", type="stdio", command="python")
    connection = FakeConnection(config)
    adapter = MCPToolAdapter("demo", remote_tool(), connection)  # type: ignore[arg-type]
    assert adapter.definition.name == "mcp__demo__lookup"
    assert adapter.definition.category == "read"
    assert adapter.definition.should_defer is True
    assert adapter.definition.permission is not None
    result = await adapter.execute({}, ToolContext(cwd=tmp_path, timeout_seconds=10))
    assert not result.is_error
    assert result.content.startswith("first\n[已忽略非文本 MCP 内容: ImageContent]\nlast")
    assert "secret-data" not in result.content


@pytest.mark.asyncio
async def test_manager_isolates_invalid_names_and_closes_connections() -> None:
    configs = [MCPServerConfig(name="one", type="stdio", command="python")]
    created: list[FakeConnection] = []

    def factory(config: MCPServerConfig) -> FakeConnection:
        connection = FakeConnection(config, [remote_tool(), remote_tool("bad.name")])
        created.append(connection)
        return connection

    manager = MCPClientManager(configs, connection_factory=factory)  # type: ignore[arg-type]
    registry = ToolRegistry()
    issues = await manager.initialize(registry)
    assert registry.get("mcp__one__lookup")
    assert any(issue.source == "tool_name" for issue in issues)
    await manager.close()
    assert created[0].closed


@pytest.mark.asyncio
async def test_manager_registers_server_metadata_for_deferred_prompt() -> None:
    config = MCPServerConfig(name="grafana_prod", type="stdio", command="python")

    def factory(server_config: MCPServerConfig) -> FakeConnection:
        server_info = types.Implementation.model_validate(
            {
                "name": "grafana",
                "title": "Grafana MCP",
                "version": "1.0.0",
                "description": "查询监控指标和日志",
            }
        )
        return FakeConnection(server_config, [remote_tool()], server_info)

    manager = MCPClientManager([config], connection_factory=factory)  # type: ignore[arg-type]
    registry = ToolRegistry()

    await manager.initialize(registry)

    assert registry.list_deferred_index() == [
        DeferredToolGroup(
            server_name="grafana_prod",
            title="Grafana MCP",
            description="查询监控指标和日志",
            tool_names=("mcp__grafana_prod__lookup",),
        )
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_manager_falls_back_to_config_name_and_omits_missing_description() -> None:
    config = MCPServerConfig(name="server_with_underscores", type="stdio", command="python")
    manager = MCPClientManager(
        [config],
        connection_factory=lambda server_config: FakeConnection(
            server_config,
            [remote_tool()],
            types.Implementation(name="remote", version="1.0.0"),
        ),
    )  # type: ignore[arg-type]
    registry = ToolRegistry()

    await manager.initialize(registry)

    group = registry.list_deferred_index()[0]
    assert group.title == "server_with_underscores"
    assert group.description is None
    await manager.close()


@pytest.mark.asyncio
async def test_manager_times_out_one_server_without_losing_other() -> None:
    class SlowConnection(FakeConnection):
        async def connect_and_list_tools(self) -> MCPServerDiscovery:
            if self.name == "slow":
                await asyncio.sleep(1)
            return MCPServerDiscovery(self.server_info, (remote_tool(self.name),))

    configs = [
        MCPServerConfig(name="slow", type="stdio", command="python"),
        MCPServerConfig(name="fast", type="stdio", command="python"),
    ]
    manager = MCPClientManager(configs, timeout_seconds=0.01, connection_factory=SlowConnection)  # type: ignore[arg-type]
    registry = ToolRegistry()
    await manager.initialize(registry)
    assert registry.get("mcp__fast__fast")
    assert any(issue.server_name == "slow" for issue in manager.issues)
    await manager.close()


@pytest.mark.asyncio
async def test_manager_reports_each_server_progress_independently() -> None:
    class StaggeredConnection(FakeConnection):
        async def connect_and_list_tools(self) -> MCPServerDiscovery:
            await asyncio.sleep(0.03 if self.name == "slow" else 0)
            return MCPServerDiscovery(self.server_info, (remote_tool(self.name),))

    configs = [
        MCPServerConfig(name="slow", type="stdio", command="python"),
        MCPServerConfig(name="fast", type="stdio", command="python"),
    ]
    manager = MCPClientManager(configs, connection_factory=StaggeredConnection)  # type: ignore[arg-type]
    snapshots = []
    manager.add_progress_callback(snapshots.append)

    await manager.initialize(ToolRegistry())

    assert any(
        {server.name: server.state for server in progress.servers}
        == {"slow": "connecting", "fast": "ready"}
        for progress in snapshots
    )
    final = snapshots[-1]
    assert final.state == "complete"
    assert [(server.name, server.state, server.registered_tools) for server in final.servers] == [
        ("slow", "ready", 1),
        ("fast", "ready", 1),
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_real_stdio_server_discovery_call_and_close(tmp_path: Path) -> None:
    server_path = Path(__file__).with_name("stdio_test_server.py")
    config = MCPServerConfig(
        name="stdio_test", type="stdio", command=sys.executable, args=[str(server_path)]
    )
    connection = MCPServerConnection(config)
    discovery = await asyncio.wait_for(connection.connect_and_list_tools(), timeout=10)
    assert discovery.server_info.name
    assert [tool.name for tool in discovery.tools] == ["echo"]
    adapter = MCPToolAdapter("stdio_test", discovery.tools[0], connection)
    result = await adapter.execute(
        {"value": "你好 MCP"}, ToolContext(cwd=tmp_path, timeout_seconds=10)
    )
    assert result.content == "你好 MCP"
    await asyncio.wait_for(connection.close(), timeout=5)


@pytest.mark.asyncio
async def test_adapter_failure_writes_redacted_error_log(tmp_path: Path) -> None:
    class FailedConnection(FakeConnection):
        async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
            raise RuntimeError("Authorization: Bearer private-token")

    log_path = tmp_path / "lancher-error.log"
    configure_logging(log_path=log_path)
    register_sensitive_values(["private-token"])
    try:
        config = MCPServerConfig(name="demo", type="stdio", command="python")
        adapter = MCPToolAdapter("demo", remote_tool(), FailedConnection(config))  # type: ignore[arg-type]
        result = await adapter.execute(
            {"secret_argument": "must-not-be-logged"},
            ToolContext(cwd=tmp_path, timeout_seconds=10),
        )
    finally:
        close_logging()
    log_text = log_path.read_text(encoding="utf-8")
    assert result.error_code == "mcp_tool_error"
    assert "event=mcp_tool_call_failed server=demo tool=lookup" in log_text
    assert "RuntimeError" in log_text
    assert "private-token" not in log_text
    assert "must-not-be-logged" not in log_text
