from __future__ import annotations

import pytest

from lancher_code.errors import ToolNotFoundError
from lancher_code.contracts.tools import ToolDefinition
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
    def __init__(self, name: str, description: str, *, allowed_phases=("discuss", "plan", "execute")) -> None:
        self._definition = ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object"},
            should_defer=True,
            allowed_phases=allowed_phases,
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
        DeferredTool("mcp__demo__write", "远程写入", allowed_phases=("execute",))
    )

    assert registry.search_deferred("write", work_phase="plan") == []


def test_registry_groups_deferred_tools_without_parsing_visible_names() -> None:
    registry = ToolRegistry()
    registry.register_deferred_server(
        "grafana_prod",
        title="Grafana MCP",
        description="查询监控指标",
    )
    registry.register(
        DeferredTool("mcp__grafana_prod__query", "查询指标"),
        deferred_server_name="grafana_prod",
    )
    registry.register(
        DeferredTool("mcp__grafana_prod__write", "写入指标", allowed_phases=("execute",)),
        deferred_server_name="grafana_prod",
    )

    groups = registry.list_deferred_index(work_phase="plan")

    assert len(groups) == 1
    assert groups[0].server_name == "grafana_prod"
    assert groups[0].tool_names == ("mcp__grafana_prod__query",)


def test_registry_search_includes_server_metadata_and_prioritizes_exact_names() -> None:
    registry = ToolRegistry()
    registry.register_deferred_server("db", title="Inventory", description="库存商品查询")
    registry.register(DeferredTool("mcp__db__lookup", "普通读取"), deferred_server_name="db")
    registry.register(DeferredTool("mcp__db__other", "可以替代 mcp__db__lookup"), deferred_server_name="db")
    assert len(registry.search_deferred("库存")) == 2
    assert registry.search_deferred("mcp__db__lookup")[0].name == "mcp__db__lookup"


def test_registry_catalog_replace_is_atomic_on_conflict_and_unregisters_removed_tools() -> None:
    registry = ToolRegistry()
    old = DeferredTool("mcp__demo__old", "old")
    registry.register_deferred_server("demo", title="Demo", description=None)
    registry.register(old, deferred_server_name="demo")
    registry.register(DeferredTool("reserved", "other"))
    with pytest.raises(ValueError):
        registry.replace_deferred_server("demo", [DeferredTool("reserved", "new")], title="New", description=None)
    assert registry.get("mcp__demo__old") is old
    replacement = DeferredTool("mcp__demo__new", "new")
    registry.replace_deferred_server("demo", [replacement], title="New", description="更新")
    assert registry.get("mcp__demo__new") is replacement
    with pytest.raises(ToolNotFoundError):
        registry.get("mcp__demo__old")
    registry.unregister_deferred_server("demo")
    assert registry.list_deferred_index() == []
    assert registry.get("reserved")
