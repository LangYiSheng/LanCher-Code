from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from lancher_code.mcp.config import MCPServerConfig

SessionFactory = Callable[[Any, Any], ClientSession]


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
        self._ready: asyncio.Future[list[types.Tool]] | None = None

    async def connect_and_list_tools(self) -> list[types.Tool]:
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
            except Exception as exc:
                raise MCPConnectionError("连接", self.name) from exc
            try:
                self._session = await stack.enter_async_context(self._session_factory(read, write))
                await self._session.initialize()
            except Exception as exc:
                raise MCPConnectionError("初始化", self.name) from exc
            try:
                tools = list((await self._session.list_tools()).tools)
            except Exception as exc:
                raise MCPConnectionError("列出工具", self.name) from exc
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(tools)
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
            httpx.AsyncClient(headers=self.config.headers, follow_redirects=True)
        )
        streams = await stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=client)
        )
        return streams[0], streams[1]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> types.CallToolResult:
        if self._session is None:
            raise RuntimeError(f"MCP Server {self.name} 已断开")
        return await self._session.call_tool(name, arguments=arguments)

    async def close(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        if self._session is None or (self._ready is not None and not self._ready.done()):
            task.cancel()
        else:
            self._close_event.set()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
