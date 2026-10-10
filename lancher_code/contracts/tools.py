from __future__ import annotations

from dataclasses import dataclass, field
from lancher_code.contracts.control import WorkPhase
from typing import Literal


ToolCategory = Literal["read", "write", "command"]


ToolSource = Literal["builtin", "external"]


@dataclass(slots=True, frozen=True)
class ToolPermissionMetadata:
    source: ToolSource
    rule_key: str
    display_name: str
    server_name: str | None = None
    remote_tool_name: str | None = None


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)
    category: ToolCategory = "read"
    is_system_tool: bool = False
    should_defer: bool = False
    allowed_phases: tuple[WorkPhase, ...] = ("execute",)
    permission: ToolPermissionMetadata | None = None


def tool_available_in_phase(tool: ToolDefinition, work_phase: WorkPhase) -> bool:
    """发现和执行统一使用工具明确声明的阶段范围。"""
    return work_phase in tool.allowed_phases


@dataclass(slots=True)
class ToolCallChunk:
    call_index: int
    provider_call_id: str | None = None
    name_delta: str = ""
    arguments_delta: str = ""


@dataclass(slots=True, frozen=True)
class DeferredToolGroup:
    server_name: str
    title: str
    description: str | None
    tool_names: tuple[str, ...]


@dataclass(slots=True)
class ToolCall:
    call_index: int
    call_id: str
    tool_name: str
    arguments: dict[str, object]
    arguments_json: str


@dataclass(slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    content: str = ""
    is_error: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None
