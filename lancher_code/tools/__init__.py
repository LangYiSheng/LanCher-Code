from __future__ import annotations

from lancher_code.tools.builtin import (
    BashTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
    WritePlanFileTool,
)
from lancher_code.tools.core.registry import ToolRegistry


def create_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(BashTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(WritePlanFileTool())
    return registry


__all__ = ["ToolRegistry", "create_default_tool_registry"]
