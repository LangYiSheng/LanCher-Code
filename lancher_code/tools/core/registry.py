from __future__ import annotations

from lancher_code.errors import ToolNotFoundError
from lancher_code.models import DeferredToolGroup, RuntimeMode, ToolDefinition
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
        mode: RuntimeMode | None = None,
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
            if mode is not None and mode not in tool.definition.allowed_modes:
                continue
            definitions.append(tool.definition)
        return definitions

    def list_deferred_index(self, *, mode: RuntimeMode | None = None) -> list[DeferredToolGroup]:
        grouped_names: dict[str, list[str]] = {}
        for definition in self.list_definitions(include_deferred=True, mode=mode):
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
        mode: RuntimeMode | None = None,
        limit: int = 8,
    ) -> list[ToolDefinition]:
        normalized = query.strip()
        deferred = [
            definition
            for definition in self.list_definitions(include_deferred=True, mode=mode)
            if definition.should_defer
        ]
        if normalized.casefold().startswith("select:"):
            selected_name = normalized.split(":", 1)[1].strip()
            return [definition for definition in deferred if definition.name == selected_name]

        terms = normalized.casefold().split()
        if not terms:
            return []
        matches = [
            definition
            for definition in deferred
            if all(
                term in f"{definition.name} {definition.description}".casefold()
                for term in terms
            )
        ]
        return matches[:limit]
