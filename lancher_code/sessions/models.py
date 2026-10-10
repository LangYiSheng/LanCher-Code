from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from lancher_code.context.models import CompactionActivity, ContextManagementState
from lancher_code.contracts.control import PermissionPolicy, WorkPhase
from lancher_code.usage.models import MessageUsage


MessageRole = Literal["system", "user", "assistant"]

MessageStatus = Literal["streaming", "complete", "error", "cancelled"]

PlanModeEntryKind = Literal["initial", "reentry"]

TraceEntryKind = Literal["thinking", "tool_call", "tool_result", "text", "notice", "compaction"]


@dataclass(slots=True)
class TraceEntry:
    kind: TraceEntryKind
    text: str = ""
    call_id: str = ""
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    ok: bool | None = None


@dataclass(slots=True)
class ThinkingTrace:
    entries: list[TraceEntry] = field(default_factory=list)
    collapsed: bool = True


@dataclass(slots=True)
class SessionMessage:
    id: str
    role: MessageRole
    content: str
    status: MessageStatus
    timestamp: datetime
    usage: MessageUsage = field(default_factory=MessageUsage)
    trace: ThinkingTrace = field(default_factory=ThinkingTrace)


@dataclass(slots=True)
class PlanSnapshot:
    content: str
    digest: str
    source_message_id: str
    ready: bool = False

    @classmethod
    def create(cls, content: str, source_message_id: str, *, ready: bool = False) -> "PlanSnapshot":
        return cls(content, hashlib.sha256(content.encode("utf-8")).hexdigest(), source_message_id, ready)


@dataclass(slots=True)
class PendingInput:
    id: str
    text: str
    delivery: Literal["follow_up", "steer"] = "follow_up"
    target_task_id: str | None = None
    state: Literal["pending", "paused"] = "pending"


@dataclass(slots=True)
class SessionState:
    messages: list[SessionMessage] = field(default_factory=list)
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"
    session_id: str | None = None
    plan_snapshot: PlanSnapshot | None = None
    pending_inputs: list[PendingInput] = field(default_factory=list)
    plan_mode_turn_count: int = 0
    pending_plan_exit_notice: bool = False
    pending_plan_entry_kind: PlanModeEntryKind | None = None
    context_management: ContextManagementState = field(default_factory=ContextManagementState)
    # 用量属于实际请求尝试，不能从压缩后的对话内容反推。
    request_usage: dict[str, dict[str, object]] = field(default_factory=dict)
    compaction_activities: dict[str, CompactionActivity] = field(default_factory=dict)
    execution: dict[str, object] = field(default_factory=lambda: {
        "processes": {}, "invocations": {}, "inbox": [],
    })

    def snapshot(self) -> list[SessionMessage]:
        return list(self.messages)
