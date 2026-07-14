from __future__ import annotations

import pytest

from lancher_code.errors import ToolNotFoundError
from lancher_code.models import ToolDefinition
from lancher_code.tools.builtin.read_file import ReadFileTool
from lancher_code.tools.core.registry import ToolRegistry


def test_registry_registers_and_lists_tools() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool()

    registry.register(tool)

    assert registry.get("read_file") is tool
    assert [definition.name for definition in registry.list_definitions()] == ["read_file"]


def test_registry_rejects_duplicate_registration() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    with pytest.raises(ValueError):
        registry.register(ReadFileTool())


def test_registry_raises_for_missing_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError):
        registry.get("missing")


class DeferredTool:
    def __init__(self, name: str, description: str, *, allowed_modes=("default", "plan", "acceptEdits", "bypass")) -> None:
        self._definition = ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object"},
            should_defer=True,
            allowed_modes=allowed_modes,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments, context):  # pragma: no cover - 注册表测试无需执行
        raise NotImplementedError


def test_registry_lists_only_explicitly_discovered_deferred_tools() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(DeferredTool("mcp__grafana__query_prometheus", "查询 Prometheus 指标"))
    registry.register(DeferredTool("mcp__grafana__query_loki", "查询 Loki 日志"))

    assert [item.name for item in registry.list_definitions()] == ["read_file"]
    assert [
        item.name
        for item in registry.list_definitions(discovered_names={"mcp__grafana__query_loki"})
    ] == ["read_file", "mcp__grafana__query_loki"]


def test_registry_searches_deferred_tools_by_keyword_and_exact_name() -> None:
    registry = ToolRegistry()
    registry.register(DeferredTool("mcp__grafana__query_prometheus", "查询 Prometheus 指标"))
    registry.register(DeferredTool("mcp__grafana__query_loki", "查询 Loki 日志"))

    assert [item.name for item in registry.search_deferred("PROMETHEUS")] == [
        "mcp__grafana__query_prometheus"
    ]
    assert [item.name for item in registry.search_deferred("select:mcp__grafana__query_loki")] == [
        "mcp__grafana__query_loki"
    ]
    assert registry.search_deferred("select:mcp__grafana__missing") == []


def test_registry_excludes_mode_disallowed_deferred_tools() -> None:
    registry = ToolRegistry()
    registry.register(
        DeferredTool("mcp__demo__write", "远程写入", allowed_modes=("default",))
    )

    assert registry.search_deferred("write", mode="plan") == []
