from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from lancher_code.contracts.control import PermissionPolicy, WorkPhase
from lancher_code.contracts.messages import ConversationMessage
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.sessions.models import PlanModeEntryKind, PlanSnapshot


@dataclass(slots=True)
class PromptContext:
    cwd: Path
    current_date: date
    plan_file_path: Path | None
    os_label: str
    plan_mode_turn_count: int = 0
    pending_plan_entry_kind: PlanModeEntryKind | None = None
    pending_plan_exit_notice: bool = False
    plan_exists: bool = False
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"
    plan_snapshot: PlanSnapshot | None = None
    session_id: str | None = None
    session_workspace: Path | None = None


@dataclass(slots=True)
class PromptPayload:
    system: list[str] = field(default_factory=list)
    messages: list[ConversationMessage] = field(default_factory=list)
    tools: list[ToolDefinition] = field(default_factory=list)
