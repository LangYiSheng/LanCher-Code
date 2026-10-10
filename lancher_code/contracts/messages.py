from __future__ import annotations

from dataclasses import dataclass, field
from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.control import PermissionPolicy, WorkPhase
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.providers.models import ProviderProtocol
from lancher_code.providers.models import ThinkingConfig
from lancher_code.usage.models import MessageUsage
from typing import Callable
from typing import Literal


ConversationRole = Literal["system", "user", "assistant", "tool"]


StreamEventKind = Literal[
    "text_delta",
    "thinking_delta",
    "tool_call_delta",
    "message_start",
    "message_end",
    "error",
]


ContentBlockKind = Literal["text", "tool_use", "tool_result", "thinking", "redacted_thinking"]


@dataclass(slots=True)
class ContentBlock:
    kind: ContentBlockKind
    text: str = ""
    call_id: str = ""
    name: str = ""
    input: dict[str, object] = field(default_factory=dict)
    is_error: bool = False
    # 协议思考属于单次助手响应，签名与加密数据不能由展示轨迹重建。
    signature: str | None = None
    data: str | None = None
    thinking_protocol: ProviderProtocol | None = None
    thinking_field: Literal["reasoning_content", "reasoning"] | None = None

    @classmethod
    def text_block(cls, text: str) -> ContentBlock:
        return cls(kind="text", text=text)

    @classmethod
    def thinking_block(
        cls, text: str, *, signature: str | None = None, protocol: ProviderProtocol = "claude",
        thinking_field: Literal["reasoning_content", "reasoning"] | None = None,
    ) -> ContentBlock:
        return cls(kind="thinking", text=text, signature=signature, thinking_protocol=protocol,
                   thinking_field=thinking_field)

    @classmethod
    def redacted_thinking_block(cls, data: str) -> ContentBlock:
        return cls(kind="redacted_thinking", data=data, thinking_protocol="claude")

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
    response_protocol: ProviderProtocol | None = None
    response_model: str | None = None

    @classmethod
    def text_message(cls, role: ConversationRole, text: str) -> ConversationMessage:
        return cls(role=role, blocks=[ContentBlock.text_block(text)])

    @classmethod
    def text_blocks_message(cls, role: ConversationRole, texts: list[str]) -> ConversationMessage:
        return cls(role=role, blocks=[ContentBlock.text_block(text) for text in texts])


@dataclass(slots=True)
class ChatRequest:
    model: str
    system: list[str] = field(default_factory=list)
    messages: list[ConversationMessage] = field(default_factory=list)
    tools: list[ToolDefinition] = field(default_factory=list)
    allow_tool_calls: bool = True
    thinking: ThinkingConfig | None = None
    cancellation_token: CancellationToken | None = None
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"
    session_id: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    purpose: str = "chat"
    usage_callback: Callable[[dict[str, object]], None] | None = None
    max_output_tokens: int | None = None
    request_id: str | None = None
    run_id: str | None = None
    # 实验协议通过历史位置追加工具定义；常规端点继续发送完整 tools 数组。
    experimental_mcp_tool_append: bool = False
    tool_updates: list[dict[str, object]] = field(default_factory=list)
    prompt_cache_enabled: bool = False
    _prepared_usage_attempt_id: str | None = field(default=None, init=False, repr=False)

    def prepare_usage_attempt(self) -> None:
        """标记已经分配身份的本次请求，供供应商消费一次。"""
        self._prepared_usage_attempt_id = self.request_id

    def take_prepared_usage_attempt(self) -> str | None:
        identity = self._prepared_usage_attempt_id
        self._prepared_usage_attempt_id = None
        return identity


@dataclass(slots=True)
class StreamEvent:
    kind: StreamEventKind
    text: str | None = None
    usage: MessageUsage = field(default_factory=MessageUsage)
    tool_call_chunk: ToolCallChunk | None = None
    stop_reason: str | None = None
    # 只在提供方完整结束时携带；按本次响应的协议顺序保留，独立于 UI trace。
    assistant_blocks: list[ContentBlock] | None = None
    # 只有显式完整结束的响应可以执行工具。
    response_complete: bool = False
