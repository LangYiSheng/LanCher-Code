from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.contracts.tools import ToolPermissionMetadata

_BUILTIN_LABELS = {
    "read_file": "ReadFile", "write_file": "WriteFile", "edit_file": "EditFile",
    "run_command": "RunCommand", "glob": "Glob", "grep": "Grep", "write_plan_file": "WritePlanFile",
    "tool_search": "ToolSearch",
    "process_list": "ProcessList", "process_read": "ProcessRead", "process_wait": "ProcessWait",
    "process_write": "ProcessWrite", "process_stop": "ProcessStop", "process_background": "ProcessBackground",
}


def create_default_tool_registry() -> ToolRegistry:
    from lancher_code.tools.core.registry import ToolRegistry
    from lancher_code.tools.builtin import (
        RunCommandTool,
        create_process_tools,
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
    registry.register(RunCommandTool())
    for process_tool in create_process_tools():
        registry.register(process_tool)
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


__all__ = ["create_default_tool_registry"]
