from __future__ import annotations

from typing import Protocol

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition:
        """返回暴露给模型的工具定义。"""

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> ToolExecutionResult:
        """执行工具并返回结构化结果。"""


def build_tool_success(
    *,
    content: str,
    summary: str,
    metadata: dict[str, object] | None = None,
    tool_name: str = "",
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id="",
        tool_name=tool_name,
        content=content,
        is_error=False,
        metadata=metadata or {},
        summary=summary,
    )


def build_tool_error(
    *,
    content: str | None = None,
    summary: str,
    error_code: str,
    error_message: str,
    metadata: dict[str, object] | None = None,
    tool_name: str = "",
) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id="",
        tool_name=tool_name,
        content=content or error_message,
        is_error=True,
        metadata=metadata or {},
        summary=summary,
        error_code=error_code,
        error_message=error_message,
    )
