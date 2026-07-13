from __future__ import annotations

from mcp import types as mcp_types

from lancher_code.mcp.connection import MCPServerConnection
from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult, ToolPermissionMetadata
from lancher_code.logging_system import get_logger

logger = get_logger("mcp.adapter")


class MCPToolAdapter:
    def __init__(self, server_name: str, remote: mcp_types.Tool, connection: MCPServerConnection) -> None:
        self._server_name = server_name
        self._remote = remote
        self._connection = connection
        self._remote_name = remote.name
        read_only = bool(remote.annotations and remote.annotations.readOnlyHint is True)
        visible_name = f"mcp__{server_name}__{remote.name}"
        schema = remote.inputSchema if isinstance(remote.inputSchema, dict) else None
        self._definition = ToolDefinition(
            name=visible_name,
            description=remote.description or f"来自 MCP Server {server_name} 的工具 {remote.name}",
            params_model=dict(schema or {"type": "object", "properties": {}}),
            category="read" if read_only else "command",
            is_concurrency_safe=read_only,
            is_system_tool=False,
            should_defer=False,
            permission=ToolPermissionMetadata(
                source="external",
                rule_key=visible_name,
                display_name=f"MCP {server_name}/{remote.name}",
                server_name=server_name,
                remote_tool_name=remote.name,
            ),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        del context
        try:
            result = await self._connection.call_tool(self._remote_name, arguments)
        except Exception:
            logger.exception(
                "event=mcp_tool_call_failed server=%s tool=%s",
                self._server_name, self._remote_name,
            )
            return ToolExecutionResult(
                call_id="", tool_name=self.definition.name,
                content=f"MCP 工具 {self._server_name}/{self._remote_name} 调用失败或连接已断开。",
                is_error=True, summary="MCP 工具调用失败", error_code="mcp_tool_error",
                error_message="MCP 工具调用失败",
                metadata={"server": self._server_name, "remote_tool": self._remote_name},
            )
        content, block_types = _extract_content(result.content)
        is_error = bool(result.isError)
        return ToolExecutionResult(
            call_id="", tool_name=self.definition.name, content=content, is_error=is_error,
            summary="MCP 工具返回错误" if is_error else "MCP 工具调用完成",
            error_code="mcp_remote_error" if is_error else None,
            error_message="MCP Server 返回错误" if is_error else None,
            metadata={"server": self._server_name, "remote_tool": self._remote_name, "content_types": block_types},
        )


def _extract_content(content: list[object]) -> tuple[str, list[str]]:
    parts: list[str] = []
    block_types: list[str] = []
    for block in content:
        block_type = type(block).__name__
        block_types.append(block_type)
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[已忽略非文本 MCP 内容: {block_type}]")
    if not parts:
        return "(MCP 工具没有返回文本内容)", block_types
    return "\n".join(parts), block_types
