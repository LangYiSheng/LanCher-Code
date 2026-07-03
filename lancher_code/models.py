from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

ProviderProtocol = Literal["openai", "claude"]
MessageRole = Literal["system", "user", "assistant"]
ConversationRole = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "error", "cancelled"]
RuntimeMode = Literal["normal", "plan"]
PlanModeEntryKind = Literal["initial", "reentry"]
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
    "mode_changed",
    "turn_cancelled",
    "assistant_message_completed",
    "turn_failed",
]
ContentBlockKind = Literal["text", "tool_use", "tool_result"]
TraceEntryKind = Literal["thinking", "tool_call", "tool_result", "text", "notice"]
ToolCategory = Literal["read", "write", "command"]


@dataclass(slots=True)
class ThinkingConfig:
    enabled: bool = False
    budget_tokens: int | None = None


@dataclass(slots=True)
class UIConfig:
    show_timestamps: bool = False
    show_thinking_status: bool = True


@dataclass(slots=True)
class RuntimeConfig:
    tool_loop_limit: int = 50
    unknown_tool_streak_limit: int = 3
    plan_file_path: str = "./.lancher/plan.md"


@dataclass(slots=True)
class ProviderConfig:
    protocol: ProviderProtocol
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    thinking: ThinkingConfig | None = None


@dataclass(slots=True)
class AppConfig:
    provider: ProviderConfig
    ui: UIConfig = field(default_factory=UIConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


@dataclass(slots=True)
class MessageUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True, init=False)
class ToolDefinition:
    name: str
    description: str
    params_model: dict[str, object]
    category: ToolCategory
    is_concurrency_safe: bool = True
    is_system_tool: bool = False
    should_defer: bool = False
    allowed_modes: tuple[RuntimeMode, ...] = ("normal", "plan")

    def __init__(
        self,
        name: str,
        description: str,
        params_model: dict[str, object] | None = None,
        category: ToolCategory = "read",
        is_concurrency_safe: bool = True,
        is_system_tool: bool = False,
        should_defer: bool = False,
        allowed_modes: tuple[RuntimeMode, ...] = ("normal", "plan"),
        input_schema: dict[str, object] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.params_model = params_model or input_schema or {}
        self.category = category
        self.is_concurrency_safe = is_concurrency_safe
        self.is_system_tool = is_system_tool
        self.should_defer = should_defer
        self.allowed_modes = allowed_modes

    @property
    def input_schema(self) -> dict[str, object]:
        return self.params_model


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
    mode: RuntimeMode = "normal"
    plan_file_path: Path | None = None
    cancellation_token: CancellationToken | None = None
    file_state_cache: "FileStateCache | None" = None

    def __post_init__(self) -> None:
        if self.file_state_cache is None:
            from lancher_code.tools.core.file_state_cache import FileStateCache

            self.file_state_cache = FileStateCache()


@dataclass(slots=True)
class ToolCallChunk:
    call_index: int
    provider_call_id: str | None = None
    name_delta: str = ""
    arguments_delta: str = ""


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


@dataclass(slots=True)
class SessionState:
    messages: list[SessionMessage] = field(default_factory=list)
    runtime_mode: RuntimeMode = "normal"
    previous_runtime_mode: RuntimeMode | None = None
    plan_mode_turn_count: int = 0
    pending_plan_exit_notice: bool = False
    pending_plan_entry_kind: PlanModeEntryKind | None = None

    def snapshot(self) -> list[SessionMessage]:
        return list(self.messages)


@dataclass(slots=True)
class PromptContext:
    cwd: Path
    current_date: date
    runtime_mode: RuntimeMode
    plan_file_path: Path
    os_label: str
    previous_runtime_mode: RuntimeMode | None = None
    plan_mode_turn_count: int = 0
    pending_plan_entry_kind: PlanModeEntryKind | None = None
    pending_plan_exit_notice: bool = False
    plan_exists: bool = False


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
    mode: RuntimeMode = "normal"
    cancellation_token: CancellationToken | None = None


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
