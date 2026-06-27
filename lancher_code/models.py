from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ProviderProtocol = Literal["openai", "claude"]
MessageRole = Literal["system", "user", "assistant"]
MessageStatus = Literal["streaming", "complete", "error"]
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
class MessageUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class ApiMessage:
    role: MessageRole
    content: str


@dataclass(slots=True)
class SessionMessage:
    id: str
    role: MessageRole
    content: str
    status: MessageStatus
    timestamp: datetime
    usage: MessageUsage = field(default_factory=MessageUsage)
    thinking: str = ""


@dataclass(slots=True)
class SessionState:
    messages: list[SessionMessage] = field(default_factory=list)

    def snapshot(self) -> list[SessionMessage]:
        return list(self.messages)


@dataclass(slots=True)
class ChatRequest:
    model: str
    messages: list[ApiMessage]
    thinking: ThinkingConfig | None = None


@dataclass(slots=True)
class StreamEvent:
    kind: StreamEventKind
    text: str | None = None
    usage: MessageUsage = field(default_factory=MessageUsage)
