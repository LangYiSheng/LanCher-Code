from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ProviderProtocol = Literal["openai", "claude"]
MessageRole = Literal["system", "user", "assistant"]
StreamEventKind = Literal[
    "text_delta",
    "thinking_delta",
    "message_start",
    "message_end",
    "error",
]


@dataclass(slots=True)
class ThinkingConfig:
    enabled: bool = False
    budget_tokens: int | None = None


@dataclass(slots=True)
class UIConfig:
    show_timestamps: bool = False
    show_thinking_status: bool = True


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


@dataclass(slots=True)
class ChatMessage:
    role: MessageRole
    content: str


@dataclass(slots=True)
class SessionState:
    messages: list[ChatMessage] = field(default_factory=list)

    def add_message(self, role: MessageRole, content: str) -> None:
        self.messages.append(ChatMessage(role=role, content=content))

    def snapshot(self) -> list[ChatMessage]:
        return list(self.messages)


@dataclass(slots=True)
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingConfig | None = None


@dataclass(slots=True)
class StreamEvent:
    kind: StreamEventKind
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
