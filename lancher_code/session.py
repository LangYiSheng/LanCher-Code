from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from lancher_code.models import (
    ChatRequest,
    ContentBlock,
    ConversationMessage,
    MessageUsage,
    ProviderConfig,
    SessionMessage,
    SessionState,
    ThinkingConfig,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    TraceEntry,
)
from lancher_code.prompting import build_system_prompt


class SessionController:
    """管理当前进程内的会话状态与协议无关 transcript。"""

    def __init__(
        self,
        provider_config: ProviderConfig,
        state: SessionState | None = None,
        *,
        cwd: Path | None = None,
        current_date: date | None = None,
    ) -> None:
        self._provider_config = provider_config
        self._state = state or SessionState()
        self._cwd = (cwd or Path.cwd()).resolve()
        self._current_date = current_date or datetime.now().astimezone().date()
        self._transcript: list[ConversationMessage] = [
            ConversationMessage.text_message(
                "system",
                build_system_prompt(cwd=self._cwd, current_date=self._current_date),
            )
        ]

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def transcript(self) -> list[ConversationMessage]:
        return list(self._transcript)

    def create_user_message(self, text: str) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="user",
            content=text,
            status="complete",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        self._transcript.append(ConversationMessage.text_message("user", text))
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

    def append_trace_thinking(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        entries = message.trace.entries
        if entries and entries[-1].kind == "thinking":
            entries[-1].text += delta
        else:
            entries.append(TraceEntry(kind="thinking", text=delta))
        return message

    def append_trace_text(self, message_id: str, text: str) -> SessionMessage:
        message = self.get_message(message_id)
        if text:
            message.trace.entries.append(TraceEntry(kind="text", text=text))
        return message

    def append_trace_notice(self, message_id: str, text: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.trace.entries.append(TraceEntry(kind="notice", text=text))
        return message

    def append_trace_tool_calls(self, message_id: str, tool_calls: list[ToolCall]) -> SessionMessage:
        message = self.get_message(message_id)
        for call in tool_calls:
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_call",
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                )
            )
        return message

    def append_trace_tool_results(self, message_id: str, results: list[ToolExecutionResult]) -> SessionMessage:
        message = self.get_message(message_id)
        for result in results:
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_result",
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    text=result.summary if result.ok else (result.error_message or result.summary),
                    metadata=result.metadata,
                    ok=result.ok,
                )
            )
        return message

    def append_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        if not tool_calls:
            return

        blocks = [
            ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name, input=call.arguments)
            for call in tool_calls
        ]
        self._transcript.append(ConversationMessage(role="assistant", blocks=blocks))

    def append_tool_results(self, results: list[ToolExecutionResult]) -> None:
        for result in results:
            self._transcript.append(
                ConversationMessage(
                    role="tool",
                    blocks=[
                        ContentBlock.tool_result_block(
                            call_id=result.call_id,
                            text=self._tool_result_content(result),
                            is_error=result.is_error,
                        )
                    ],
                )
            )

    def complete_message(self, message_id: str, usage: MessageUsage | None = None) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "complete"
        message.usage = usage or MessageUsage()
        if message.content.strip():
            self._transcript.append(ConversationMessage.text_message("assistant", message.content))
        return message

    def fail_message(self, message_id: str, error_text: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "error"
        message.content = error_text
        message.usage = MessageUsage()
        return message

    def get_message(self, message_id: str) -> SessionMessage:
        for message in self._state.messages:
            if message.id == message_id:
                return message
        raise KeyError(f"未找到消息 {message_id}")

    def build_request(
        self,
        tools: list[ToolDefinition],
        *,
        allow_tool_calls: bool,
    ) -> ChatRequest:
        thinking = self._request_thinking()
        return ChatRequest(
            model=self._provider_config.model,
            messages=self.transcript,
            tools=tools if allow_tool_calls else [],
            allow_tool_calls=allow_tool_calls,
            thinking=thinking,
        )

    def total_usage(self) -> MessageUsage:
        total = MessageUsage()
        for message in self._state.messages:
            total.input_tokens += message.usage.input_tokens
            total.output_tokens += message.usage.output_tokens
        return total

    def _request_thinking(self) -> ThinkingConfig | None:
        if self._provider_config.protocol != "claude":
            return None
        return self._provider_config.thinking

    @staticmethod
    def _tool_result_content(result: ToolExecutionResult) -> str:
        if result.ok:
            return result.content.strip() or result.summary
        return result.error_message or result.content or result.summary

    @staticmethod
    def _new_message_id() -> str:
        return uuid4().hex

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
