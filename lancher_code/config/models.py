from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lancher_code.contracts.control import PermissionPolicy, WorkPhase, validate_runtime_axes
from lancher_code.execution.contracts import ExecutionConfig
from lancher_code.providers.models import ProviderDefinition


BusyEnterAction = Literal["follow_up", "steer", "draft"]


@dataclass(slots=True)
class UIConfig:
    show_timestamps: bool = False
    show_thinking_status: bool = True
    theme: Literal["dark", "light"] = "dark"
    busy_enter_action: BusyEnterAction = "follow_up"


@dataclass(slots=True)
class RuntimeConfig:
    tool_loop_limit: int = 50
    unknown_tool_streak_limit: int = 3
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"
    experimental_mcp_tool_append: bool = False

    def __post_init__(self) -> None:
        if type(self.experimental_mcp_tool_append) is not bool:
            raise ValueError("runtime.experimental_mcp_tool_append 必须是布尔值。")
        self.work_phase, self.permission_policy = validate_runtime_axes(self.work_phase, self.permission_policy)


@dataclass(slots=True)
class AppConfig:
    providers: dict[str, ProviderDefinition]
    default_model: str
    ui: UIConfig = field(default_factory=UIConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
