from __future__ import annotations

from lancher_code.errors import ToolNotFoundError
from lancher_code.contracts.tools import DeferredToolGroup, ToolDefinition, tool_available_in_phase
from lancher_code.contracts.control import WorkPhase
from lancher_code.tools.core.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._deferred_servers: dict[str, tuple[str, str | None]] = {}
        self._deferred_tool_servers: dict[str, str] = {}

    def register(self, tool: Tool, *, deferred_server_name: str | None = None) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"工具已注册: {name}")
        self._tools[name] = tool
        if deferred_server_name is not None:
            self._deferred_tool_servers[name] = deferred_server_name

    def register_deferred_server(
        self,
        server_name: str,
        *,
        title: str,
        description: str | None,
    ) -> None:
        self._deferred_servers[server_name] = (title, description)

    def replace_deferred_server(
        self, server_name: str, tools: list[Tool], *, title: str, description: str | None,
    ) -> None:
        """先完整校验再同步替换目录，调用方不会观察到半更新的工具集合。"""
        names = [tool.definition.name for tool in tools]
        old_names = {name for name, server in self._deferred_tool_servers.items() if server == server_name}
        if len(set(names)) != len(names):
            raise ValueError(f"MCP Server {server_name} 返回了重复工具名")
        if any(name in self._tools and name not in old_names for name in names):
            raise ValueError(f"MCP Server {server_name} 的工具名称冲突")
        self.unregister_deferred_server(server_name)
        self.register_deferred_server(server_name, title=title, description=description)
        for tool in tools:
            self.register(tool, deferred_server_name=server_name)

    def unregister_deferred_server(self, server_name: str) -> None:
        for name, source in tuple(self._deferred_tool_servers.items()):
            if source == server_name:
                self._tools.pop(name, None)
                self._deferred_tool_servers.pop(name, None)
        self._deferred_servers.pop(server_name, None)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"未找到工具: {name}")
        return tool

    def list_definitions(
        self,
        *,
        include_deferred: bool = False,
        discovered_names: set[str] | None = None,
        work_phase: WorkPhase | None = None,
    ) -> list[ToolDefinition]:
        discovered = discovered_names or set()
        definitions: list[ToolDefinition] = []
        for tool in self._tools.values():
            if (
                tool.definition.should_defer
                and not include_deferred
                and tool.definition.name not in discovered
            ):
                continue
            if work_phase is not None:
                if not tool_available_in_phase(tool.definition, work_phase):
                    continue
            definitions.append(tool.definition)
        return definitions

    def list_deferred_index(self, *, work_phase: WorkPhase | None = None) -> list[DeferredToolGroup]:
        grouped_names: dict[str, list[str]] = {}
        for definition in self.list_definitions(include_deferred=True, work_phase=work_phase):
            if not definition.should_defer:
                continue
            server_name = self._deferred_tool_servers.get(definition.name)
            if server_name is None or server_name not in self._deferred_servers:
                continue
            grouped_names.setdefault(server_name, []).append(definition.name)

        return [
            DeferredToolGroup(
                server_name=server_name,
                title=self._deferred_servers[server_name][0],
                description=self._deferred_servers[server_name][1],
                tool_names=tuple(tool_names),
            )
            for server_name, tool_names in grouped_names.items()
        ]

    def search_deferred(
        self,
        query: str,
        *,
        work_phase: WorkPhase | None = None,
        limit: int = 8,
    ) -> list[ToolDefinition]:
        normalized = query.strip()
        deferred = [
            definition
            for definition in self.list_definitions(include_deferred=True, work_phase=work_phase)
            if definition.should_defer
        ]
        if normalized.casefold().startswith("select:"):
            selected_name = normalized.split(":", 1)[1].strip()
            return [definition for definition in deferred if definition.name == selected_name]

        terms = normalized.casefold().split()
        if not terms:
            return []
        ranked: list[tuple[int, str, ToolDefinition]] = []
        for definition in deferred:
            server = self._deferred_tool_servers.get(definition.name, "")
            title, description = self._deferred_servers.get(server, ("", None))
            name = definition.name.casefold()
            searchable = f"{name} {definition.description} {server} {title} {description or ''}".casefold()
            if not all(term in searchable for term in terms):
                continue
            # 完整名称最优先，其次名称/服务器匹配，再考虑用途描述。
            score = 10000 if name == normalized.casefold() else 0
            score += sum(100 if term in name else 20 if term in f"{server} {title}".casefold() else 1 for term in terms)
            ranked.append((-score, name, definition))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in ranked[:max(0, limit)]]
