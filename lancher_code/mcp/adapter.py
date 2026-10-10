from __future__ import annotations

import asyncio
import json

from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from lancher_code.mcp.connection import MCPServerConnection
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.validation import validate_tool_arguments
from lancher_code.contracts.tools import ToolDefinition, ToolExecutionResult, ToolPermissionMetadata
from lancher_code.logging_system import get_logger

logger = get_logger("mcp.adapter")


class MCPToolAdapter:
    def __init__(self, server_name: str, remote: mcp_types.Tool, connection: MCPServerConnection) -> None:
        self._server_name = server_name
        self._remote = remote
        self._connection = connection
        self._remote_name = remote.name
        self.timeout_seconds = getattr(getattr(connection, "config", None), "tool_timeout_seconds", 60.0)
        read_only = bool(remote.annotations and remote.annotations.readOnlyHint is True)
        visible_name = f"mcp__{server_name}__{remote.name}"
        schema = remote.inputSchema if isinstance(remote.inputSchema, dict) else None
        self._definition = ToolDefinition(
            name=visible_name,
            description=remote.description or f"来自 MCP Server {server_name} 的工具 {remote.name}",
            input_schema=dict(schema or {"type": "object", "properties": {}}),
            category="read" if read_only else "command",
            allowed_phases=("discuss", "plan", "execute") if read_only else ("execute",),
            is_system_tool=False,
            should_defer=True,
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
            async with asyncio.timeout(self.timeout_seconds):
                result = await self._connection.call_tool(self._remote_name, arguments)
        except Exception as exc:
            if isinstance(exc, McpError) and exc.error.code not in {mcp_types.CONNECTION_CLOSED, 408}:
                return ToolExecutionResult(
                    call_id="", tool_name=self.definition.name, is_error=True,
                    content=f"MCP Server 返回协议错误: {exc.error.message}", summary="MCP 远端协议错误",
                    error_code="mcp_remote_error", error_message=exc.error.message,
                    metadata={"server": self._server_name, "remote_tool": self._remote_name,
                              "remote_error_code": exc.error.code, "automatic_retry": False},
                )
            logger.exception(
                "event=mcp_tool_call_failed server=%s tool=%s",
                self._server_name, self._remote_name,
            )
            unknown = self.definition.category != "read"
            message = (f"MCP 工具 {self._server_name}/{self._remote_name} 连接中断，远端结果无法确认。"
                       "操作可能已经发生，请先查询实际状态，再决定是否重试。" if unknown else
                       f"MCP 工具 {self._server_name}/{self._remote_name} 调用失败或连接已断开。")
            return ToolExecutionResult(
                call_id="", tool_name=self.definition.name,
                content=message, is_error=True, summary="远端结果未知" if unknown else "MCP 工具调用失败",
                error_code="mcp_outcome_unknown" if unknown else "mcp_tool_error", error_message=message,
                metadata={"server": self._server_name, "remote_tool": self._remote_name,
                          "outcome_unknown": unknown, "automatic_retry": False},
            )
        content, block_types, resources = _extract_content(result.content)
        is_error = bool(result.isError)
        metadata: dict[str, object] = {
            "server": self._server_name, "remote_tool": self._remote_name,
            "content_types": block_types, "resources": resources,
            "automatic_retry": False,
        }
        if result.structuredContent is not None:
            metadata["structured_content"] = result.structuredContent
            structured_text = json.dumps(result.structuredContent, ensure_ascii=False, indent=2)
            content = f"{content}\n结构化结果：\n{structured_text}" if result.content else structured_text
        if self._remote.outputSchema is not None:
            metadata["output_schema"] = self._remote.outputSchema
            if not is_error:
                # 输出契约复用离线校验器，服务器的 $ref 不能触发额外网络或本地文件读取。
                issue = validate_tool_arguments(result.structuredContent, self._remote.outputSchema)
                if issue is not None:
                    # 远端已有响应，结果不符合契约不能当作传输失败，也不能自动重放。
                    metadata["output_schema_valid"] = False
                    message = "MCP Server 返回的结构化结果未通过 outputSchema 校验。"
                    return ToolExecutionResult(
                        call_id="", tool_name=self.definition.name,
                        content=f"{message}\n{content}", is_error=True, summary="MCP 输出校验失败",
                        error_code="mcp_output_invalid", error_message=message, metadata=metadata,
                    )
                metadata["output_schema_valid"] = True
        return ToolExecutionResult(
            call_id="", tool_name=self.definition.name, content=content, is_error=is_error,
            summary="MCP 工具返回错误" if is_error else "MCP 工具调用完成",
            error_code="mcp_remote_error" if is_error else None,
            error_message="MCP Server 返回错误" if is_error else None,
            metadata=metadata,
        )


def _extract_content(content: list[object]) -> tuple[str, list[str], list[dict[str, object]]]:
    parts: list[str] = []
    block_types: list[str] = []
    resources: list[dict[str, object]] = []
    for block in content:
        block_type = type(block).__name__
        block_types.append(block_type)
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.ResourceLink):
            resource = block.model_dump(mode="json", exclude_none=True)
            resources.append(resource)
            parts.append(f"资源链接: {block.title or block.name} ({block.uri})" +
                         (f"\n{block.description}" if block.description else ""))
        elif isinstance(block, mcp_types.EmbeddedResource) and isinstance(block.resource, mcp_types.TextResourceContents):
            resource = block.resource.model_dump(mode="json", exclude_none=True)
            resources.append(resource)
            parts.append(f"资源文本: {block.resource.uri}\n{block.resource.text}")
        else:
            parts.append(f"[尚未投影非文本 MCP 内容: {block_type}；原始内容未展示]")
    if not parts:
        return "(MCP 工具没有返回文本内容)", block_types, resources
    return "\n".join(parts), block_types, resources
