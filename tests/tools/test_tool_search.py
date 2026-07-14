from __future__ import annotations

import pytest

from lancher_code.models import ToolContext, ToolDefinition
from lancher_code.tools.builtin.tool_search import ToolSearchTool
from lancher_code.tools.core.registry import ToolRegistry


class DeferredTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="mcp__grafana__query_prometheus",
            description="查询 Prometheus 指标",
            input_schema={"type": "object"},
            should_defer=True,
        )

    async def execute(self, arguments, context):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_tool_search_returns_discovered_names(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(DeferredTool())
    tool = ToolSearchTool(registry)

    result = await tool.execute(
        {"query": "prometheus"},
        ToolContext(cwd=tmp_path, timeout_seconds=1),
    )

    assert not result.is_error
    assert result.metadata["discovered_tool_names"] == ["mcp__grafana__query_prometheus"]
    assert "完整参数定义将在下一次模型请求中提供" in result.content


@pytest.mark.asyncio
async def test_tool_search_returns_structured_errors(tmp_path) -> None:
    tool = ToolSearchTool(ToolRegistry())
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)

    invalid = await tool.execute({"query": "  "}, context)
    missing = await tool.execute({"query": "unknown"}, context)

    assert invalid.error_code == "invalid_arguments"
    assert missing.error_code == "deferred_tool_not_found"


@pytest.mark.asyncio
async def test_tool_search_requires_narrower_query_when_result_limit_is_exceeded(tmp_path) -> None:
    class ManyDeferredTool:
        def __init__(self, index: int) -> None:
            self._definition = ToolDefinition(
                name=f"mcp__demo__lookup_{index}",
                description="通用查询",
                input_schema={"type": "object"},
                should_defer=True,
            )

        @property
        def definition(self) -> ToolDefinition:
            return self._definition

        async def execute(self, arguments, context):  # pragma: no cover
            raise NotImplementedError

    registry = ToolRegistry()
    for index in range(9):
        registry.register(ManyDeferredTool(index))

    result = await ToolSearchTool(registry).execute(
        {"query": "lookup"},
        ToolContext(cwd=tmp_path, timeout_seconds=1),
    )

    assert result.error_code == "too_many_deferred_tools"
    assert result.metadata["result_limit"] == 8
