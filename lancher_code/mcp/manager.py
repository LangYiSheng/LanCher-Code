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
from lancher_code.logging_system import get_logger

logger = get_logger("mcp.manager")

TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
ConnectionFactory = Callable[[MCPServerConfig], MCPServerConnection]


@dataclass(slots=True, frozen=True)
class MCPServerInitialization:
    name: str
    state: str
    registered_tools: int = 0
    warning_count: int = 0


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
    servers: tuple[MCPServerInitialization, ...] = ()


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
        self._server_states: dict[str, MCPServerInitialization] = {
            config.name: MCPServerInitialization(config.name, "waiting")
            for config in self.configs
        }

    @property
    def has_servers(self) -> bool:
        return bool(self.configs)

    def add_progress_callback(self, callback: Callable[[MCPInitializationProgress], None]) -> None:
        self._progress_callbacks.append(callback)

    async def initialize(self, registry: ToolRegistry) -> list[MCPConfigIssue]:
        self._emit(None, "initializing")
        tasks = [asyncio.create_task(self._discover_safely(config)) for config in self.configs]
        for completed in asyncio.as_completed(tasks):
            config, result = await completed
            if isinstance(result, BaseException):
                stage = result.stage if isinstance(result, MCPConnectionError) else "启动"
                self.issues.append(MCPConfigIssue(stage, f"MCP Server {config.name} {stage}失败", config.name))
                logger.error(
                    "event=mcp_server_initialization_failed server=%s stage=%s exception_type=%s",
                    config.name, stage, type(result).__name__,
                    exc_info=(type(result), result, result.__traceback__),
                )
                self._failed += 1
                self._completed += 1
                self._set_server(config.name, "failed")
                self._emit(config.name, "server_failed")
                continue
            connection, tools = result
            self._connections[config.name] = connection
            self._set_server(config.name, "registering")
            self._emit(config.name, "registering_tools")
            registered_before = self._registered
            for remote in tools:
                self._register_tool(registry, config.name, remote, connection)
            registered_tools = self._registered - registered_before
            self._successful += 1
            self._completed += 1
            warning_count = sum(1 for issue in self.issues if issue.server_name == config.name)
            self._set_server(config.name, "ready", registered_tools, warning_count)
            self._emit(config.name, "server_ready")
        self._emit(None, "complete")
        return list(self.issues)

    async def _discover_safely(
        self, config: MCPServerConfig
    ) -> tuple[MCPServerConfig, tuple[MCPServerConnection, list[mcp_types.Tool]] | BaseException]:
        try:
            return config, await self._discover(config)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return config, exc

    async def _discover(self, config: MCPServerConfig) -> tuple[MCPServerConnection, list[mcp_types.Tool]]:
        connection = self._connection_factory(config)
        self._connections[config.name] = connection
        self._set_server(config.name, "connecting")
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
            logger.error("event=mcp_tool_name_invalid server=%s", server_name)
            return
        try:
            registry.register(MCPToolAdapter(server_name, remote, connection))
        except ValueError:
            self.issues.append(MCPConfigIssue("duplicate_tool", f"Server {server_name} 的工具 {remote.name} 名称冲突", server_name))
            logger.error("event=mcp_tool_name_duplicate server=%s tool=%s", server_name, remote.name)
            return
        self._registered += 1

    def _emit(self, current_server: str | None, state: str) -> None:
        progress = MCPInitializationProgress(
            len(self.configs), self._completed, self._successful, self._failed,
            self._registered, current_server, state, len(self.issues),
            tuple(self._server_states.values()),
        )
        for callback in tuple(self._progress_callbacks):
            callback(progress)

    def _set_server(
        self,
        name: str,
        state: str,
        registered_tools: int = 0,
        warning_count: int = 0,
    ) -> None:
        self._server_states[name] = MCPServerInitialization(
            name, state, registered_tools, warning_count
        )

    async def close(self) -> None:
        connections = list(dict.fromkeys(self._connections.values()))
        self._connections.clear()
        tasks = [asyncio.create_task(connection.close()) for connection in connections]
        if not tasks:
            return
        try:
            async with asyncio.timeout(self.close_timeout_seconds):
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for connection, result in zip(connections, results):
                    if isinstance(result, BaseException):
                        logger.error(
                            "event=mcp_close_failed server=%s exception_type=%s",
                            connection.name, type(result).__name__,
                            exc_info=(type(result), result, result.__traceback__),
                        )
        except TimeoutError:
            logger.error("event=mcp_close_timeout connection_count=%d", len(connections))
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
