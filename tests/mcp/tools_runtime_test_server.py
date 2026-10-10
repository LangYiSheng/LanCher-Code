"""用于真实传输验证分页、目录通知及慢调用的本地 MCP 服务。"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server


class TestServer(Server):
    def create_initialization_options(self, notification_options=None, experimental_capabilities=None):
        return super().create_initialization_options(
            NotificationOptions(tools_changed=True), experimental_capabilities
        )


server = TestServer("tools-runtime-test", version="1.0.0")
changed = False


def tool(name: str) -> types.Tool:
    return types.Tool(name=name, description=name, inputSchema={"type": "object"},
                      annotations=types.ToolAnnotations(readOnlyHint=name != "mutate"))


@server.list_tools()
async def list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
    cursor = request.params.cursor if request is not None and request.params is not None else None
    if cursor is None:
        return types.ListToolsResult(tools=[tool("echo"), tool("mutate"), tool("slow")], nextCursor="second")
    return types.ListToolsResult(tools=[tool("new" if changed else "old")])


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    global changed
    if name == "mutate":
        changed = True
        await server.request_context.session.send_tool_list_changed()
    if name == "slow":
        await asyncio.sleep(0.15)
    if name == "echo" and arguments.get("exit_after"):
        async def exit_after_reply():
            await asyncio.sleep(0.05)
            os._exit(0)
        asyncio.create_task(exit_after_reply())
    return types.CallToolResult(content=[types.TextContent(type="text", text="done")],
                                structuredContent={"tool": name})


async def run_stdio() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run_http(port: int) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(server)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=manager.handle_request)], lifespan=lifespan)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_http(int(sys.argv[1]))
    else:
        asyncio.run(run_stdio())
