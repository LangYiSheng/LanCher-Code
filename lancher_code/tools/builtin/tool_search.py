from __future__ import annotations

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.registry import ToolRegistry

TOOL_SEARCH_RESULT_LIMIT = 8


class ToolSearchTool:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="tool_search",
            description=(
                "搜索并加载当前隐藏的 MCP 工具。"
                "已知完整工具名时使用 select:<完整工具名> 精确加载；"
                "否则输入工具名、MCP Server 名或用途关键词。"
            ),
            params_model={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，或 select:mcp__server__tool。",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            category="read",
            is_concurrency_safe=True,
            is_system_tool=True,
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return build_tool_error(
                summary="搜索 MCP 工具失败",
                error_code="invalid_arguments",
                error_message="query 必须是非空字符串。",
                tool_name=self.definition.name,
            )

        matches = self._registry.search_deferred(
            query,
            mode=context.mode,
            limit=TOOL_SEARCH_RESULT_LIMIT + 1,
        )
        if not matches:
            message = "没有找到可在当前模式使用的 MCP 工具，请更换关键词或检查完整工具名。"
            return build_tool_error(
                summary="未找到 MCP 工具",
                error_code="deferred_tool_not_found",
                error_message=message,
                metadata={"query": query.strip()},
                tool_name=self.definition.name,
            )
        if len(matches) > TOOL_SEARCH_RESULT_LIMIT:
            message = (
                f"匹配的 MCP 工具超过 {TOOL_SEARCH_RESULT_LIMIT} 个，"
                "请使用更具体的关键词或 select:<完整工具名>。"
            )
            return build_tool_error(
                summary="MCP 工具搜索结果过多",
                error_code="too_many_deferred_tools",
                error_message=message,
                metadata={"query": query.strip(), "result_limit": TOOL_SEARCH_RESULT_LIMIT},
                tool_name=self.definition.name,
            )

        names = [definition.name for definition in matches]
        lines = ["已加载以下 MCP 工具，其完整参数定义将在下一次模型请求中提供："]
        lines.extend(f"- {definition.name}: {definition.description}" for definition in matches)
        return build_tool_success(
            summary=f"已加载 {len(matches)} 个 MCP 工具",
            content="\n".join(lines),
            metadata={"query": query.strip(), "discovered_tool_names": names},
            tool_name=self.definition.name,
        )
