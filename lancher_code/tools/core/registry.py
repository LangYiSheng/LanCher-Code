from __future__ import annotations

from lancher_code.errors import ToolNotFoundError
from lancher_code.models import RuntimeMode, ToolDefinition
from lancher_code.tools.core.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"工具已注册: {name}")
        self._tools[name] = tool

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

    def list_deferred_index(self, *, mode: RuntimeMode | None = None) -> list[str]:
        return [
            definition.name
            for definition in self.list_definitions(include_deferred=True, mode=mode)
            if definition.should_defer
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
