from __future__ import annotations

from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.models import ToolPermissionMetadata

_BUILTIN_LABELS = {
    "read_file": "ReadFile", "write_file": "WriteFile", "edit_file": "EditFile",
    "bash": "Bash", "glob": "Glob", "grep": "Grep", "write_plan_file": "WritePlanFile",
    "tool_search": "ToolSearch",
}


def create_default_tool_registry() -> ToolRegistry:
    from lancher_code.tools.builtin import (
        BashTool,
        EditFileTool,
        GlobTool,
        GrepTool,
        ReadFileTool,
        WriteFileTool,
        WritePlanFileTool,
        ToolSearchTool,
    )

    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(BashTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(WritePlanFileTool())
    registry.register(ToolSearchTool(registry))
    for definition in registry.list_definitions(include_deferred=True):
        if definition.permission is None:
            label = _BUILTIN_LABELS[definition.name]
            definition.permission = ToolPermissionMetadata(
                source="builtin", rule_key=definition.name, display_name=label
            )
    return registry


__all__ = ["ToolRegistry", "create_default_tool_registry"]
