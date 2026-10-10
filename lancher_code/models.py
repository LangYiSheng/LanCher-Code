from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TYPE_CHECKING
from uuid import uuid4

from lancher_code.execution.contracts import ExecutionConfig

if TYPE_CHECKING:
    from lancher_code.execution.runtime import ExecutionRuntime
    from lancher_code.execution.scheduler import ResourceLease

ProviderProtocol = Literal["openai", "claude"]
MessageRole = Literal["system", "user", "assistant"]
ConversationRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "cancelled"]
RuntimeMode = Literal["default", "plan", "acceptEdits", "bypass"]
WorkPhase = Literal["discuss", "plan", "execute"]
PermissionPolicy = Literal["default", "acceptEdits", "bypass"]
PermissionMatchKind = Literal["exact", "glob", "legacy"]
BusyEnterAction = Literal["follow_up", "steer", "draft"]
PlanModeEntryKind = Literal["initial", "reentry"]
RuleScope = Literal["session", "project", "user"]
PermissionDecision = Literal["allow", "deny", "ask"]
PermissionRuleResult = Literal["allow", "deny"]
PermissionRequestKind = Literal["command", "file_edit", "external_tool"]
PermissionResolutionOutcome = Literal["allow_once", "allow_session", "allow_project", "deny", "superseded"]
StreamEventKind = Literal[
    "text_delta",
    "thinking_delta",
    "tool_call_delta",
    "message_start",
    "message_end",
    "error",
]
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
    "policy_changed",
    "pending_input_changed",
    "steering_applied",
    "permission_request_closed",
    "turn_completed",
]
ContentBlockKind = Literal["text", "tool_use", "tool_result"]
TraceEntryKind = Literal["thinking", "tool_call", "tool_result", "text", "notice"]
ToolCategory = Literal["read", "write", "command"]
ToolSource = Literal["builtin", "external"]


def resolve_runtime_axes(
    mode: RuntimeMode | None = None,
    work_phase: WorkPhase | None = None,
    permission_policy: PermissionPolicy | None = None,
) -> tuple[WorkPhase, PermissionPolicy]:
    """仅在旧调用边界把混合模式拆成两轴，新参数优先。"""
    if (work_phase is None or permission_policy is None) and mode is not None and (
        not isinstance(mode, str) or mode not in {"default", "plan", "acceptEdits", "bypass"}
    ):
        raise ValueError("旧运行模式无效。")
    phase = work_phase if work_phase is not None else ("plan" if mode == "plan" else "execute")
    policy = permission_policy if permission_policy is not None else (mode if mode in {"acceptEdits", "bypass"} else "default")
    if not isinstance(phase, str) or phase not in {"discuss", "plan", "execute"}:
        raise ValueError("工作阶段无效。")
    if not isinstance(policy, str) or policy not in {"default", "acceptEdits", "bypass"}:
        raise ValueError("权限策略无效。")
    return phase, policy  # type: ignore[return-value]


def legacy_runtime_mode(work_phase: WorkPhase, permission_policy: PermissionPolicy) -> RuntimeMode:
    """只读兼容投影，安全判定不得依赖此值。"""
    return "plan" if work_phase == "plan" else permission_policy


@dataclass(slots=True, frozen=True)
class ToolPermissionMetadata:
    source: ToolSource
    rule_key: str
    display_name: str
    server_name: str | None = None
    remote_tool_name: str | None = None


@dataclass(slots=True)
class ThinkingConfig:
    enabled: bool = False
    budget_tokens: int | None = None


@dataclass(slots=True)
class UIConfig:
    show_timestamps: bool = False
    show_thinking_status: bool = True
    theme: Literal["dark", "light"] = "dark"
    busy_enter_action: BusyEnterAction = "follow_up"


@dataclass(slots=True, init=False)
class RuntimeConfig:
    tool_loop_limit: int = 50
    unknown_tool_streak_limit: int = 3
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"

    def __init__(self, tool_loop_limit: int = 50, unknown_tool_streak_limit: int = 3,
                 permission_mode: RuntimeMode | None = None,
                 *, work_phase: WorkPhase | None = None, permission_policy: PermissionPolicy | None = None) -> None:
        self.tool_loop_limit = tool_loop_limit
        self.unknown_tool_streak_limit = unknown_tool_streak_limit
        self.work_phase, self.permission_policy = resolve_runtime_axes(permission_mode, work_phase, permission_policy)

    @property
    def permission_mode(self) -> RuntimeMode:
        return legacy_runtime_mode(self.work_phase, self.permission_policy)


@dataclass(slots=True)
class ProviderConfig:
    protocol: ProviderProtocol
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    thinking: ThinkingConfig | None = None
    context_window: int = 128000


@dataclass(slots=True)
class ModelDefinition:
    """保存用户填写的模型配置；可选连接字段为 None 时继承供应商。"""

    model_name: str
    display_name: str = ""
    protocol: ProviderProtocol | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float | None = None
    context_window: int | None = None
    thinking: ThinkingConfig | None = None


@dataclass(slots=True)
class ProviderDefinition:
    name: str
    protocol: ProviderProtocol
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    models: dict[str, ModelDefinition] = field(default_factory=dict)


@dataclass(slots=True)
class AppConfig:
    # 仅兼容旧调用方读取默认模型快照，配置编辑和持久化以 providers 为准。
    provider: ProviderConfig | None = None
    ui: UIConfig = field(default_factory=UIConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    providers: dict[str, ProviderDefinition] = field(default_factory=dict)
    default_model: str = ""
    legacy_format: bool = False
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    def __post_init__(self) -> None:
        if self.providers or self.provider is None:
            return
        previous = self.provider
        self.providers = {
            "legacy": ProviderDefinition(
                name="原有供应商",
                protocol=previous.protocol,
                base_url=previous.base_url,
                api_key=previous.api_key,
                timeout_seconds=previous.timeout_seconds,
                models={
                    "default": ModelDefinition(
                        model_name=previous.model,
                        thinking=deepcopy(previous.thinking),
                        context_window=previous.context_window,
                    )
                },
            )
        }
        self.default_model = "legacy/default"
        self.legacy_format = True


@dataclass(slots=True)
class MessageUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class ContextUsageAnchor:
    token_count: int
    request_char_count: int
    system_tools_digest: str
    message_count: int
    messages_digest: str


@dataclass(slots=True)
class ToolResultReplacement:
    call_id: str
    preview: str
    relative_path: str
    original_bytes: int


@dataclass(slots=True)
class ContextFileSnapshot:
    path: str
    normalized_path: str
    content: str
    read_at: str


@dataclass(slots=True)
class ContextManagementState:
    context_id: str = field(default_factory=lambda: uuid4().hex)
    usage_anchor: ContextUsageAnchor | None = None
    seen_call_ids: set[str] = field(default_factory=set)
    replacements: dict[str, ToolResultReplacement] = field(default_factory=dict)
    recent_files: list[ContextFileSnapshot] = field(default_factory=list)
    automatic_failure_count: int = 0
    automatic_compaction_disabled: bool = False


@dataclass(slots=True, frozen=True)
class ContextCompactionResult:
    before_tokens: int
    after_tokens: int
    dropped_groups: int = 0


@dataclass(slots=True, init=False)
class ToolDefinition:
    name: str
    description: str
    params_model: dict[str, object]
    category: ToolCategory
    is_system_tool: bool = False
    should_defer: bool = False
    allowed_modes: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass")
    permission: ToolPermissionMetadata | None = None

    def __init__(
        self,
        name: str,
        description: str,
        params_model: dict[str, object] | None = None,
        category: ToolCategory = "read",
        is_system_tool: bool = False,
        should_defer: bool = False,
        allowed_modes: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass"),
        permission: ToolPermissionMetadata | None = None,
        input_schema: dict[str, object] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.params_model = params_model or input_schema or {}
        self.category = category
        self.is_system_tool = is_system_tool
        self.should_defer = should_defer
        self.allowed_modes = allowed_modes
        self.permission = permission

    @property
    def input_schema(self) -> dict[str, object]:
        return self.params_model


def tool_available_in_phase(tool: ToolDefinition, work_phase: WorkPhase) -> bool:
    """工具发现按能力筛选；本地写工具再由权限引擎检查实际资源。"""
    if tool.name == "write_plan_file":
        return work_phase == "plan"
    if tool.name in {"write_file", "edit_file"} and tool.permission is None:
        return True
    if work_phase == "execute":
        return any(mode in tool.allowed_modes for mode in ("default", "acceptEdits", "bypass"))
    if tool.name in {"process_list", "process_read", "process_wait", "process_stop"}:
        return True
    if tool.name in {"run_command", "process_write", "process_background"}:
        return False
    # MCP adapter 仅在服务端明确 readOnlyHint=true 时标记 read；缺省为 command。
    return tool.category == "read" and "plan" in tool.allowed_modes


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass(slots=True)
class ToolContext:
    cwd: Path
    timeout_seconds: float
    mode: RuntimeMode = "default"
    project_root: Path | None = None
    plan_file_path: Path | None = None
    session_id: str | None = None
    session_workspace: Path | None = None
    session_root: Path | None = None
    cancellation_token: CancellationToken | None = None
    file_state_cache: "FileStateCache | None" = None
    work_phase: WorkPhase | None = None
    permission_policy: PermissionPolicy | None = None
    execution_runtime: "ExecutionRuntime | None" = None
    turn_id: str | None = None
    invocation_id: str | None = None
    generation: int = 0
    resource_lease: "ResourceLease | None" = None

    def __post_init__(self) -> None:
        self.work_phase, self.permission_policy = resolve_runtime_axes(self.mode, self.work_phase, self.permission_policy)
        self.mode = legacy_runtime_mode(self.work_phase, self.permission_policy)
        if self.project_root is None:
            self.project_root = self.cwd.resolve()
        if self.file_state_cache is None:
            from lancher_code.tools.core.file_state_cache import FileStateCache

            self.file_state_cache = FileStateCache()


@dataclass(slots=True)
class PermissionRule:
    match: str
    result: PermissionRuleResult
    scope: RuleScope
    match_kind: PermissionMatchKind = "legacy"


@dataclass(slots=True)
class PermissionRequest:
    request_id: str
    call_id: str
    tool_name: str
    tool_label: str
    kind: PermissionRequestKind
    mode: RuntimeMode
    title: str
    prompt: str
    details: str
    command: str | None = None
    description: str | None = None
    file_paths: list[str] = field(default_factory=list)
    preview_lines: list[dict[str, str]] = field(default_factory=list)
    session_rule: str | None = None
    project_rule: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    match_kind: PermissionMatchKind = "exact"
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"


@dataclass(slots=True)
class PermissionResolution:
    request_id: str
    outcome: PermissionResolutionOutcome


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


@dataclass(slots=True, init=False)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    content: str
    is_error: bool
    metadata: dict[str, object] = field(default_factory=dict)
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        content: str | None = None,
        is_error: bool | None = None,
        metadata: dict[str, object] | None = None,
        summary: str = "",
        error_code: str | None = None,
        error_message: str | None = None,
        *,
        ok: bool | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        legacy_payload = dict(payload or {})
        if content is None and isinstance(legacy_payload.get("content"), str):
            content = legacy_payload.pop("content")
        if metadata is None:
            metadata = legacy_payload
        if is_error is None:
            is_error = not ok if ok is not None else False

        self.call_id = call_id
        self.tool_name = tool_name
        self.content = content or ""
        self.is_error = is_error
        self.metadata = metadata or {}
        self.summary = summary
        self.error_code = error_code
        self.error_message = error_message

    @property
    def ok(self) -> bool:
        return not self.is_error

    @property
    def payload(self) -> dict[str, object]:
        return {"content": self.content, **self.metadata}


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
class ContentBlock:
    kind: ContentBlockKind
    text: str = ""
    call_id: str = ""
    name: str = ""
    input: dict[str, object] = field(default_factory=dict)
    is_error: bool = False

    @classmethod
    def text_block(cls, text: str) -> ContentBlock:
        return cls(kind="text", text=text)

    @classmethod
    def tool_use_block(cls, *, call_id: str, name: str, input: dict[str, object]) -> ContentBlock:
        return cls(kind="tool_use", call_id=call_id, name=name, input=input)

    @classmethod
    def tool_result_block(cls, *, call_id: str, text: str, is_error: bool) -> ContentBlock:
        return cls(kind="tool_result", call_id=call_id, text=text, is_error=is_error)


@dataclass(slots=True)
class ConversationMessage:
    role: ConversationRole
    blocks: list[ContentBlock]

    @classmethod
    def text_message(cls, role: ConversationRole, text: str) -> ConversationMessage:
        return cls(role=role, blocks=[ContentBlock.text_block(text)])

    @classmethod
    def text_blocks_message(cls, role: ConversationRole, texts: list[str]) -> ConversationMessage:
        return cls(role=role, blocks=[ContentBlock.text_block(text) for text in texts])


@dataclass(slots=True)
class SessionMessage:
    id: str
    role: MessageRole
    content: str
    status: MessageStatus
    timestamp: datetime
    usage: MessageUsage = field(default_factory=MessageUsage)
    trace: ThinkingTrace = field(default_factory=ThinkingTrace)
    # 0 为旧版分离正文；1 表示 trace 已按输出顺序包含全部正文。
    timeline_version: int = 0


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
    previous_runtime_mode: RuntimeMode | None = None
    plan_mode_turn_count: int = 0
    pending_plan_exit_notice: bool = False
    pending_plan_entry_kind: PlanModeEntryKind | None = None
    context_management: ContextManagementState = field(default_factory=ContextManagementState)
    execution: dict[str, object] = field(default_factory=lambda: {
        "processes": {}, "invocations": {}, "inbox": [],
    })

    def snapshot(self) -> list[SessionMessage]:
        return list(self.messages)

    @property
    def runtime_mode(self) -> RuntimeMode:
        return legacy_runtime_mode(self.work_phase, self.permission_policy)

    @property
    def plan_restore_mode(self) -> PermissionPolicy:
        return self.permission_policy


@dataclass(slots=True)
class PromptContext:
    cwd: Path
    current_date: date
    runtime_mode: RuntimeMode
    plan_file_path: Path | None
    os_label: str
    previous_runtime_mode: RuntimeMode | None = None
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


@dataclass(slots=True)
class ChatRequest:
    model: str
    system: list[str] = field(default_factory=list)
    messages: list[ConversationMessage] = field(default_factory=list)
    tools: list[ToolDefinition] = field(default_factory=list)
    allow_tool_calls: bool = True
    thinking: ThinkingConfig | None = None
    mode: RuntimeMode = "default"
    cancellation_token: CancellationToken | None = None
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"


@dataclass(slots=True)
class StreamEvent:
    kind: StreamEventKind
    text: str | None = None
    usage: MessageUsage = field(default_factory=MessageUsage)
    tool_call_chunk: ToolCallChunk | None = None


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
    mode: RuntimeMode | None = None
    permission_request: PermissionRequest | None = None
    permission_resolution: PermissionResolution | None = None
    work_phase: WorkPhase | None = None
    permission_policy: PermissionPolicy | None = None
    task_id: str | None = None
    pending_input_id: str | None = None
