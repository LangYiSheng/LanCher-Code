from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass

from mcp import types as mcp_types

from lancher_code.mcp.adapter import MCPToolAdapter
from lancher_code.mcp.config import MCPConfigIssue, MCPServerConfig
from lancher_code.mcp.connection import MCPConnectionError, MCPServerConnection, MCPServerDiscovery
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
class MCPServerStatus:
    name: str
    state: str
    transport: str
    registered_tools: int = 0
    warning_count: int = 0
    title: str | None = None
    capabilities: tuple[str, ...] = ()
    last_error: str | None = None


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
    def __init__(self, configs: list[MCPServerConfig], *, issues: list[MCPConfigIssue] | None = None,
                 timeout_seconds: float = 30.0, close_timeout_seconds: float = 5.0,
                 connection_factory: ConnectionFactory = MCPServerConnection) -> None:
        self.configs = list(configs)
        self.issues = list(issues or [])
        self.timeout_seconds = timeout_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self._connection_factory = connection_factory
        self._connections: dict[str, MCPServerConnection] = {}
        self._registry: ToolRegistry | None = None
        self._closed = False
        self._initializing = False
        self._initialization_tasks: set[asyncio.Task] = set()
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._refresh_pending: set[str] = set()
        self._locks = {config.name: asyncio.Lock() for config in self.configs}
        self._progress_callbacks: list[Callable[[MCPInitializationProgress], None]] = []
        self._completed = self._successful = self._failed = self._registered = 0
        self._server_states = {config.name: MCPServerInitialization(config.name, "waiting") for config in self.configs}
        self._statuses = {config.name: MCPServerStatus(config.name, "waiting", config.type) for config in self.configs}

    @property
    def has_servers(self) -> bool:
        return bool(self.configs)

    def status(self) -> tuple[MCPServerStatus, ...]:
        """返回不含凭据、URL 和子进程参数的不可变展示快照。"""
        return tuple(self._statuses.values())

    def add_progress_callback(self, callback: Callable[[MCPInitializationProgress], None]) -> None:
        self._progress_callbacks.append(callback)

    async def initialize(self, registry: ToolRegistry) -> list[MCPConfigIssue]:
        if self._initializing:
            raise RuntimeError("MCP 已在初始化")
        if self._closed:
            raise RuntimeError("MCP Manager 已关闭")
        self._registry = registry
        self._initializing = True
        self._completed = self._successful = self._failed = self._registered = 0
        self._emit(None, "initializing")
        tasks = {asyncio.create_task(self._initialize_server(config, registry)) for config in self.configs}
        self._initialization_tasks = tasks
        try:
            for completed in asyncio.as_completed(tasks):
                await completed
            self._emit(None, "complete")
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown(registry)
            raise
        finally:
            self._initialization_tasks = set()
            self._initializing = False
        return list(self.issues)

    async def _initialize_server(self, config: MCPServerConfig, registry: ToolRegistry) -> None:
        async with self._locks[config.name]:
            if config.name in self._connections:
                self._completed += 1
                self._successful += 1
                self._registered = sum(item.registered_tools for item in self._server_states.values())
                return
            try:
                connection, discovery = await self._discover(config)
                self._set_server(config.name, "registering")
                self._emit(config.name, "registering_tools")
                self._install(registry, config.name, connection, discovery)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._remove_connection(config)
                registry.unregister_deferred_server(config.name)
                self._record_failure(config.name, exc, "启动")
                self._failed += 1
                self._completed += 1
                self._emit(config.name, "server_failed")
                return
            self._successful += 1
            self._completed += 1
            self._emit(config.name, "server_ready")

    async def _discover(self, config: MCPServerConfig) -> tuple[MCPServerConnection, MCPServerDiscovery]:
        if self._closed:
            raise RuntimeError("MCP Manager 已关闭")
        connection = self._connection_factory(config)
        self._connections[config.name] = connection
        if hasattr(connection, "add_tools_changed_callback"):
            connection.add_tools_changed_callback(lambda: self._schedule_refresh(config.name))
        if hasattr(connection, "add_disconnect_callback"):
            connection.add_disconnect_callback(lambda: self._on_disconnect(config.name, connection))
        self._set_server(config.name, "connecting")
        self._emit(config.name, "connecting")
        async with asyncio.timeout(config.startup_timeout_seconds or self.timeout_seconds):
            discovery = await connection.connect_and_list_tools()
        return connection, discovery

    def _install(self, registry: ToolRegistry, server_name: str, connection: MCPServerConnection,
                 discovery: MCPServerDiscovery) -> None:
        if self._closed:
            raise RuntimeError("MCP Manager 已关闭")
        if isinstance(connection, MCPServerConnection) and not connection.connected:
            raise RuntimeError(f"MCP Server {server_name} 已断开")
        adapters: list[MCPToolAdapter] = []
        names: set[str] = set()
        for remote in discovery.tools:
            if not remote.name or not TOOL_NAME_PATTERN.fullmatch(remote.name):
                self.issues.append(MCPConfigIssue("tool_name", f"Server {server_name} 返回了非法工具名", server_name))
                continue
            if remote.name in names:
                self.issues.append(MCPConfigIssue("duplicate_tool", f"Server {server_name} 的工具 {remote.name} 名称重复", server_name))
                continue
            names.add(remote.name)
            adapters.append(MCPToolAdapter(server_name, remote, connection))
        title = _server_title(server_name, discovery.server_info)
        registry.replace_deferred_server(server_name, adapters, title=title,
                                         description=_server_description(discovery.server_info))
        capabilities: tuple[str, ...] = ()
        if discovery.capabilities is not None and discovery.capabilities.tools is not None:
            capabilities = ("tools",) + (("tools.listChanged",) if discovery.capabilities.tools.listChanged else ())
        self._set_server(server_name, "ready", len(adapters), title=title, capabilities=capabilities)
        self._registered = sum(item.registered_tools for item in self._server_states.values())

    async def refresh(self, registry: ToolRegistry, server_name: str | None = None) -> tuple[MCPServerStatus, ...]:
        self._registry = registry
        configs = self._select_configs(server_name)
        for config in configs:
            async with self._locks[config.name]:
                connection = self._connections.get(config.name)
                if connection is None:
                    continue
                previous_status = self._statuses[config.name]
                previous_progress = self._server_states[config.name]
                self._set_server(config.name, "refreshing", self._statuses[config.name].registered_tools)
                try:
                    async with asyncio.timeout(config.startup_timeout_seconds or self.timeout_seconds):
                        discovery = await connection.refresh_tools()
                    self._install(registry, config.name, connection, discovery)
                except asyncio.CancelledError:
                    if self._closed:
                        registry.unregister_deferred_server(config.name)
                        self._set_server(config.name, "stopped")
                    elif (self._statuses[config.name].state == "disconnected"
                          or self._connections.get(config.name) is not connection
                          or not getattr(connection, "connected", True)):
                        registry.unregister_deferred_server(config.name)
                        self._set_server(config.name, "disconnected", last_error="连接已断开；请手动重连")
                    else:
                        # 发现候选未提交，原子目录仍是原先快照；取消不能留下刷新中。
                        self._statuses[config.name] = previous_status
                        self._server_states[config.name] = previous_progress
                    self._registered = sum(item.registered_tools for item in self._server_states.values())
                    self._emit(config.name, "catalog_updated")
                    raise
                except Exception as exc:
                    registry.unregister_deferred_server(config.name)
                    self._record_failure(config.name, exc, "刷新")
                self._emit(config.name, "catalog_updated")
        return self.status()

    async def reconnect(self, registry: ToolRegistry, server_name: str) -> tuple[MCPServerStatus, ...]:
        self._registry = registry
        config = self._select_configs(server_name)[0]
        async with self._locks[config.name]:
            registry.unregister_deferred_server(config.name)
            self._set_server(config.name, "connecting")
            self._registered = sum(item.registered_tools for item in self._server_states.values())
            try:
                await self._remove_connection(config)
                connection, discovery = await self._discover(config)
                self._install(registry, config.name, connection, discovery)
            except asyncio.CancelledError:
                await self._remove_connection(config)
                registry.unregister_deferred_server(config.name)
                self._set_server(config.name, "stopped" if self._closed else "disconnected",
                                 last_error=None if self._closed else "重连已取消；请重新连接")
                self._registered = sum(item.registered_tools for item in self._server_states.values())
                self._emit(config.name, "reconnected")
                raise
            except Exception as exc:
                await self._remove_connection(config)
                self._record_failure(config.name, exc, "重连")
            self._emit(config.name, "reconnected")
        return self.status()

    def _select_configs(self, server_name: str | None) -> list[MCPServerConfig]:
        if self._closed:
            raise RuntimeError("MCP Manager 已关闭")
        selected = [config for config in self.configs if server_name is None or config.name == server_name]
        if server_name is not None and not selected:
            raise ValueError(f"未配置 MCP Server: {server_name}")
        return selected

    def _schedule_refresh(self, name: str) -> None:
        if self._closed or self._registry is None:
            return
        self._refresh_pending.add(name)
        if name in self._refresh_tasks and not self._refresh_tasks[name].done():
            return
        self._refresh_tasks[name] = asyncio.create_task(self._refresh_notifications(name), name=f"mcp-refresh-{name}")

    async def _refresh_notifications(self, name: str) -> None:
        try:
            while name in self._refresh_pending and not self._closed:
                self._refresh_pending.discard(name)
                assert self._registry is not None
                await self.refresh(self._registry, name)
        finally:
            self._refresh_tasks.pop(name, None)

    def _on_disconnect(self, name: str, connection: MCPServerConnection) -> None:
        if self._closed or self._connections.get(name) is not connection:
            return
        if self._registry is not None:
            self._registry.unregister_deferred_server(name)
        self._set_server(name, "disconnected", last_error="连接已断开；请手动重连")
        self._registered = sum(item.registered_tools for item in self._server_states.values())
        self._emit(name, "disconnected")

    def _record_failure(self, name: str, exc: Exception, fallback_stage: str) -> None:
        if self._closed:
            return
        stage = exc.stage if isinstance(exc, MCPConnectionError) else fallback_stage
        message = f"MCP Server {name} {stage}失败"
        self.issues.append(MCPConfigIssue(stage, message, name))
        self._set_server(name, "failed", last_error=message)
        self._registered = sum(item.registered_tools for item in self._server_states.values())
        logger.error("event=mcp_server_operation_failed server=%s stage=%s exception_type=%s",
                     name, stage, type(exc).__name__, exc_info=(type(exc), exc, exc.__traceback__))

    def _emit(self, current_server: str | None, state: str) -> None:
        states = tuple(self._server_states.values())
        if self._initializing:
            completed, successful, failed = self._completed, self._successful, self._failed
        else:
            successful = sum(server.state == "ready" for server in states)
            failed = sum(server.state in {"failed", "disconnected"} for server in states)
            completed = sum(server.state in {"ready", "failed", "disconnected", "stopped"} for server in states)
        progress = MCPInitializationProgress(
            len(self.configs), completed, successful, failed,
            self._registered, current_server, state, len(self.issues), states,
        )
        for callback in tuple(self._progress_callbacks):
            callback(progress)

    def _set_server(self, name: str, state: str, registered_tools: int = 0, *,
                    title: str | None = None, capabilities: tuple[str, ...] | None = None,
                    last_error: str | None = None) -> None:
        warnings = sum(issue.server_name == name for issue in self.issues)
        previous = self._statuses[name]
        self._server_states[name] = MCPServerInitialization(name, state, registered_tools, warnings)
        self._statuses[name] = MCPServerStatus(
            name, state, previous.transport, registered_tools, warnings,
            title if title is not None else previous.title,
            capabilities if capabilities is not None else previous.capabilities, last_error,
        )

    async def _remove_connection(self, config: MCPServerConfig) -> None:
        connection = self._connections.pop(config.name, None)
        if connection is None:
            return
        try:
            async with asyncio.timeout(config.close_timeout_seconds or self.close_timeout_seconds):
                await connection.close()
        except Exception as exc:
            logger.error("event=mcp_close_failed server=%s exception_type=%s", config.name, type(exc).__name__)

    async def shutdown(self, registry: ToolRegistry | None = None) -> None:
        self._closed = True
        selected_registry = registry or self._registry
        current = asyncio.current_task()
        tasks = [task for task in (*self._initialization_tasks, *self._refresh_tasks.values()) if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_tasks.clear()
        self._refresh_pending.clear()
        await asyncio.gather(*(self._remove_connection(config) for config in self.configs))
        for config in self.configs:
            if selected_registry is not None:
                selected_registry.unregister_deferred_server(config.name)
            self._set_server(config.name, "stopped")
        self._registered = 0

    async def close(self) -> None:
        await self.shutdown()


def _server_title(server_name: str, server_info: mcp_types.Implementation) -> str:
    title = server_info.title
    return title.strip() if isinstance(title, str) and title.strip() else server_name


def _server_description(server_info: mcp_types.Implementation) -> str | None:
    description = getattr(server_info, "description", None)
    if description is None:
        model_extra = getattr(server_info, "model_extra", None)
        if isinstance(model_extra, dict):
            description = model_extra.get("description")
    return description.strip() if isinstance(description, str) and description.strip() else None
