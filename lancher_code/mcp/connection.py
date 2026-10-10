from __future__ import annotations

import asyncio
import os
import inspect
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from lancher_code.mcp.config import MCPServerConfig

SessionFactory = Callable[..., ClientSession]


class _ObservedReceiveStream:
    """在 SDK 读取流关闭时发出事件，不另起任务争抢协议消息。"""

    def __init__(self, stream: Any, on_disconnect: Callable[[], None]) -> None:
        self._stream = stream
        self._on_disconnect = on_disconnect

    async def __aenter__(self) -> _ObservedReceiveStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self._stream.__aexit__(*args)

    def __aiter__(self) -> _ObservedReceiveStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def receive(self) -> Any:
        try:
            return await self._stream.receive()
        except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
            self._on_disconnect()
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()


@dataclass(slots=True, frozen=True)
class MCPServerDiscovery:
    server_info: types.Implementation
    tools: tuple[types.Tool, ...]
    capabilities: types.ServerCapabilities | None = None


class MCPConnectionError(RuntimeError):
    def __init__(self, stage: str, server_name: str) -> None:
        super().__init__(f"MCP Server {server_name} {stage}失败")
        self.stage = stage
        self.server_name = server_name


class MCPServerConnection:
    def __init__(self, config: MCPServerConfig, *, session_factory: SessionFactory = ClientSession) -> None:
        self.config = config
        self.name = config.name
        self._session_factory = session_factory
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_event = asyncio.Event()
        self._ready: asyncio.Future[MCPServerDiscovery] | None = None
        self._server_info: types.Implementation | None = None
        self._capabilities: types.ServerCapabilities | None = None
        self._tools_changed_callbacks: list[Callable[[], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self._disconnected = False

    @property
    def connected(self) -> bool:
        return not self._disconnected and self._session is not None and self._task is not None and not self._task.done()

    def add_tools_changed_callback(self, callback: Callable[[], None]) -> None:
        self._tools_changed_callbacks.append(callback)

    def add_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self._disconnect_callbacks.append(callback)

    def _notify_disconnect(self) -> None:
        if self._disconnected:
            return
        self._disconnected = True
        self._close_event.set()
        for callback in tuple(self._disconnect_callbacks):
            callback()

    async def _handle_message(self, message: object) -> None:
        # 通知回调只安排刷新；在 SDK 的接收任务内等待 RPC 会阻塞响应接收。
        if isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
            if self._capabilities is not None and self._capabilities.tools is not None and self._capabilities.tools.listChanged:
                for callback in tuple(self._tools_changed_callbacks):
                    callback()
        elif isinstance(message, Exception):
            self._notify_disconnect()

    async def connect_and_list_tools(self) -> MCPServerDiscovery:
        if self._task is not None:
            raise RuntimeError(f"MCP Server {self.name} 已经启动")
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        try:
            return await self._ready
        except asyncio.CancelledError:
            await self.close()
            raise

    async def _run(self) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            try:
                read, write = await self._connect_transport(stack)
                read = _ObservedReceiveStream(read, self._notify_disconnect)
            except Exception as exc:
                raise MCPConnectionError("连接", self.name) from exc
            try:
                parameters = inspect.signature(self._session_factory).parameters
                accepts_handler = "message_handler" in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                )
                session = self._session_factory(read, write, message_handler=self._handle_message) if accepts_handler else self._session_factory(read, write)
                self._session = await stack.enter_async_context(session)
                initialize_result = await self._session.initialize()
                self._server_info = initialize_result.serverInfo
                self._capabilities = initialize_result.capabilities
            except Exception as exc:
                raise MCPConnectionError("初始化", self.name) from exc
            try:
                tools = await self.list_tools() if self._capabilities.tools is not None else ()
            except Exception as exc:
                raise MCPConnectionError("列出工具", self.name) from exc
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(MCPServerDiscovery(initialize_result.serverInfo, tools, self._capabilities))
            await self._close_event.wait()
        except asyncio.CancelledError:
            if self._ready is not None and not self._ready.done():
                self._ready.cancel()
            raise
        except BaseException as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self._session = None
            await stack.aclose()
            self._notify_disconnect()

    async def list_tools(self) -> tuple[types.Tool, ...]:
        if self._session is None or self._disconnected:
            raise RuntimeError(f"MCP Server {self.name} 已断开")
        tools: list[types.Tool] = []
        cursor: str | None = None
        seen: set[str] = set()
        supports_params = "params" in inspect.signature(self._session.list_tools).parameters
        while True:
            if supports_params:
                result = await self._session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
                )
            else:
                # 旧版 1.x SDK 只有 cursor 参数，避免抬高已有依赖的最低版本。
                result = await self._session.list_tools(cursor=cursor) if cursor is not None else await self._session.list_tools()
            tools.extend(result.tools)
            cursor = result.nextCursor
            if cursor is None:
                return tuple(tools)
            if cursor in seen:
                raise RuntimeError(f"MCP Server {self.name} 工具分页游标重复")
            seen.add(cursor)

    async def refresh_tools(self) -> MCPServerDiscovery:
        if self._server_info is None or self._session is None or self._disconnected:
            raise RuntimeError(f"MCP Server {self.name} 已断开")
        tools = await self.list_tools() if self._capabilities is None or self._capabilities.tools is not None else ()
        return MCPServerDiscovery(self._server_info, tools, self._capabilities)

    async def _connect_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.is_stdio:
            assert self.config.command is not None
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env={**os.environ, **self.config.env},
            )
            return await stack.enter_async_context(stdio_client(params))
        assert self.config.url is not None
        client = await stack.enter_async_context(
            httpx.AsyncClient(headers=self.config.headers, follow_redirects=True,
                              timeout=httpx.Timeout(self.config.tool_timeout_seconds))
        )
        streams = await stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=client)
        )
        return streams[0], streams[1]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
        if self._session is None or self._disconnected:
            raise RuntimeError(f"MCP Server {self.name} 已断开")
        # 结果校验由 Adapter 用当前目录的 outputSchema 完成。SDK 的 call_tool
        # 会把已收到的结构化校验错误抛成传输异常，造成写操作结果被误判未知。
        try:
            return await self._session.send_request(
                types.ClientRequest(types.CallToolRequest(
                    params=types.CallToolRequestParams(name=name, arguments=arguments)
                )),
                types.CallToolResult,
                request_read_timeout_seconds=timedelta(seconds=self.config.tool_timeout_seconds),
            )
        except Exception as exc:
            if not isinstance(exc, McpError) or exc.error.code in {types.CONNECTION_CLOSED, 408}:
                self._notify_disconnect()
            raise

    async def close(self) -> None:
        if self._task is None:
            return
        caller = asyncio.current_task()
        cancellations = caller.cancelling() if caller is not None else 0
        task, self._task = self._task, None
        if self._session is None or (self._ready is not None and not self._ready.done()):
            task.cancel()
        else:
            self._close_event.set()
        try:
            await task
        except asyncio.CancelledError:
            # 启动阶段主动取消子任务可以忽略；调用方自身的取消必须继续传播。
            if not task.cancelled() or (caller is not None and caller.cancelling() > cancellations):
                raise
