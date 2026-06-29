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
        mode: RuntimeMode | None = None,
    ) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for tool in self._tools.values():
            if tool.definition.should_defer and not include_deferred:
                continue
            if mode is not None and mode not in tool.definition.allowed_modes:
                continue
            definitions.append(tool.definition)
        return definitions
