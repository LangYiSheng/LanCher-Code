from __future__ import annotations

from dataclasses import dataclass, field
from lancher_code.context.models import CompactionActivity
from lancher_code.contracts.control import ExecutionAccess, WorkPhase
from lancher_code.contracts.tools import ToolCall
from lancher_code.contracts.tools import ToolExecutionResult
from lancher_code.permissions.models import PermissionRequest
from lancher_code.permissions.models import PermissionResolution
from lancher_code.sessions.models import SessionMessage
from lancher_code.usage.models import MessageUsage
from typing import Literal
from lancher_code.contracts.json import json_value


TurnEventKind = Literal[
    "user_message_created",
    "assistant_message_started",
    "assistant_text_delta",
    "tool_call_started",
    "tool_result_received",
    "usage_updated",
    "progress_updated",
    "permission_request_created",
    "permission_request_resolved",
    "turn_cancelled",
    "assistant_message_completed",
    "turn_failed",
    "phase_changed",
    "access_changed",
    "pending_input_changed",
    "steering_applied",
    "permission_request_closed",
    "turn_completed",
    "compaction_updated",
    "mcp_progress_updated",
    "session_changed",
]


@dataclass(slots=True)
class TurnEvent:
    kind: TurnEventKind
    message: SessionMessage | None = None
    usage: MessageUsage = field(default_factory=MessageUsage)
    error_text: str | None = None
    text: str | None = None
    progress_message: str | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolExecutionResult | None = None
    permission_request: PermissionRequest | None = None
    permission_resolution: PermissionResolution | None = None
    work_phase: WorkPhase | None = None
    execution_access: ExecutionAccess | None = None
    task_id: str | None = None
    pending_input_id: str | None = None
    compaction: CompactionActivity | None = None
    session_id: str | None = None
    run_id: str | None = None
    sequence: int = 0
    mcp_progress: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return json_value(self)
