from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from lancher_code.models import (
    ApiMessage,
    ChatRequest,
    MessageRole,
    MessageUsage,
    ProviderConfig,
    SessionMessage,
    SessionState,
)


class SessionController:
    """管理当前进程内的会话状态。"""

    def __init__(self, provider_config: ProviderConfig, state: SessionState | None = None) -> None:
        self._provider_config = provider_config
        self._state = state or SessionState()

    @property
    def state(self) -> SessionState:
        return self._state

    def create_user_message(self, text: str) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="user",
            content=text,
            status="complete",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        return message

    def create_assistant_message(self) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="assistant",
            content="",
            status="streaming",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        return message

    def append_message_content(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content += delta
        return message

    def append_message_thinking(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.thinking += delta
        return message

    def complete_message(self, message_id: str, usage: MessageUsage | None = None) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "complete"
        message.usage = usage or MessageUsage()
        return message

    def fail_message(self, message_id: str, error_text: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "error"
        message.content = error_text
        message.thinking = ""
        message.usage = MessageUsage()
        return message

    def get_message(self, message_id: str) -> SessionMessage:
        for message in self._state.messages:
            if message.id == message_id:
                return message
        raise KeyError(f"未找到消息: {message_id}")

    def build_request(self) -> ChatRequest:
        thinking = None
        if self._provider_config.protocol == "claude":
            thinking = self._provider_config.thinking

        return ChatRequest(
            model=self._provider_config.model,
            messages=self._to_api_messages(),
            thinking=thinking,
        )

    def total_usage(self) -> MessageUsage:
        total = MessageUsage()
        for message in self._state.messages:
            total.input_tokens += message.usage.input_tokens
            total.output_tokens += message.usage.output_tokens
        return total

    def _to_api_messages(self) -> list[ApiMessage]:
        messages: list[ApiMessage] = []
        for message in self._state.messages:
            if message.role == "assistant":
                if message.status != "complete":
                    continue
                if not message.content.strip():
                    continue
            elif message.status == "error":
                continue

            messages.append(ApiMessage(role=message.role, content=message.content))
        return messages

    @staticmethod
    def _new_message_id() -> str:
        return uuid4().hex

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
