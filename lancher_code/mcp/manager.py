from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass

from mcp import types as mcp_types

from lancher_code.mcp.adapter import MCPToolAdapter
from lancher_code.mcp.config import MCPConfigIssue, MCPServerConfig
from lancher_code.mcp.connection import MCPConnectionError, MCPServerConnection
from lancher_code.tools.core.registry import ToolRegistry

TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
ConnectionFactory = Callable[[MCPServerConfig], MCPServerConnection]


@dataclass(slots=True, frozen=True)
class MCPInitializationProgress:
    total_servers: int
    completed_servers: int
    successful_servers: int
    failed_servers: int
    registered_tools: int
    current_server: str | None
    state: str
    warning_count: int = 0


class MCPClientManager:
    def __init__(self, configs: list[MCPServerConfig], *, issues: list[MCPConfigIssue] | None = None, timeout_seconds: float = 10.0, close_timeout_seconds: float = 5.0, connection_factory: ConnectionFactory = MCPServerConnection) -> None:
        self.configs = list(configs)
        self.issues = list(issues or [])
        self.timeout_seconds = timeout_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self._connection_factory = connection_factory
        self._connections: dict[str, MCPServerConnection] = {}
        self._progress_callbacks: list[Callable[[MCPInitializationProgress], None]] = []
        self._completed = self._successful = self._failed = self._registered = 0

    @property
    def has_servers(self) -> bool:
        return bool(self.configs)

    def add_progress_callback(self, callback: Callable[[MCPInitializationProgress], None]) -> None:
        self._progress_callbacks.append(callback)

    async def initialize(self, registry: ToolRegistry) -> list[MCPConfigIssue]:
        self._emit(None, "initializing")
        tasks = [asyncio.create_task(self._discover(config)) for config in self.configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for config, result in zip(self.configs, results):
            if isinstance(result, BaseException):
                stage = result.stage if isinstance(result, MCPConnectionError) else "启动"
                self.issues.append(MCPConfigIssue(stage, f"MCP Server {config.name} {stage}失败", config.name))
                self._failed += 1
                self._completed += 1
                self._emit(config.name, "server_failed")
                continue
            connection, tools = result
            self._connections[config.name] = connection
            for remote in tools:
                self._register_tool(registry, config.name, remote, connection)
            self._successful += 1
            self._completed += 1
            self._emit(config.name, "server_ready")
        self._emit(None, "complete")
        return list(self.issues)

    async def _discover(self, config: MCPServerConfig) -> tuple[MCPServerConnection, list[mcp_types.Tool]]:
        connection = self._connection_factory(config)
        self._connections[config.name] = connection
        self._emit(config.name, "connecting")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                tools = await connection.connect_and_list_tools()
            return connection, tools
        except BaseException:
            await connection.close()
            self._connections.pop(config.name, None)
            raise

    def _register_tool(self, registry: ToolRegistry, server_name: str, remote: mcp_types.Tool, connection: MCPServerConnection) -> None:
        if not remote.name or not TOOL_NAME_PATTERN.fullmatch(remote.name):
            self.issues.append(MCPConfigIssue("tool_name", f"Server {server_name} 返回了非法工具名", server_name))
            return
        try:
            registry.register(MCPToolAdapter(server_name, remote, connection))
        except ValueError:
            self.issues.append(MCPConfigIssue("duplicate_tool", f"Server {server_name} 的工具 {remote.name} 名称冲突", server_name))
            return
        self._registered += 1

    def _emit(self, current_server: str | None, state: str) -> None:
        progress = MCPInitializationProgress(
            len(self.configs), self._completed, self._successful, self._failed,
            self._registered, current_server, state, len(self.issues)
        )
        for callback in tuple(self._progress_callbacks):
            callback(progress)

    async def close(self) -> None:
        connections = list(dict.fromkeys(self._connections.values()))
        self._connections.clear()
        tasks = [asyncio.create_task(connection.close()) for connection in connections]
        if not tasks:
            return
        try:
            async with asyncio.timeout(self.close_timeout_seconds):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
