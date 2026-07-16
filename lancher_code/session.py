from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from lancher_code.models import (
    ChatRequest,
    ContentBlock,
    ConversationMessage,
    DeferredToolGroup,
    MessageUsage,
    ProviderConfig,
    PromptContext,
    RuntimeMode,
    SessionMessage,
    SessionState,
    ThinkingConfig,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    TraceEntry,
)
from lancher_code.prompting import (
    build_chat_request_payload,
    build_dynamic_context_prompt,
    build_prompt_context,
    build_user_message,
)


class SessionController:
    """管理当前进程内的会话状态与协议无关 transcript。"""

    def __init__(
        self,
        provider_config: ProviderConfig,
        state: SessionState | None = None,
        *,
        cwd: Path | None = None,
        current_date: date | None = None,
        plan_file_path: Path | None = None,
        initial_runtime_mode: RuntimeMode = "default",
    ) -> None:
        self._provider_config = provider_config
        self._state = state or SessionState()
        self._cwd = (cwd or Path.cwd()).resolve()
        self._current_date = current_date or datetime.now().astimezone().date()
        self._plan_file_path = self._resolve_plan_file_path(plan_file_path)
        self._transcript: list[ConversationMessage] = []
        if initial_runtime_mode != self._state.runtime_mode:
            self.set_runtime_mode(initial_runtime_mode)

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def transcript(self) -> list[ConversationMessage]:
        return list(self._transcript)

    @property
    def runtime_mode(self) -> RuntimeMode:
        return self._state.runtime_mode

    @property
    def plan_file_path(self) -> Path:
        return self._plan_file_path

    def set_runtime_mode(self, mode: RuntimeMode) -> RuntimeMode:
        previous_mode = self._state.runtime_mode
        if mode == previous_mode:
            return mode

        self._state.previous_runtime_mode = previous_mode
        if mode == "plan" and previous_mode != "plan":
            self._state.plan_restore_mode = previous_mode
            self._state.pending_plan_entry_kind = "reentry" if self._plan_file_path.exists() else "initial"
            self._state.pending_plan_exit_notice = False
            self._state.plan_mode_turn_count = 0
        elif previous_mode == "plan" and mode != "plan":
            self._state.pending_plan_exit_notice = self._state.plan_mode_turn_count > 0
            self._state.pending_plan_entry_kind = None

        self._state.runtime_mode = mode
        return mode

    def restore_mode_after_plan(self) -> RuntimeMode:
        restore_mode = self._state.plan_restore_mode
        self.set_runtime_mode(restore_mode)
        return restore_mode

    def create_user_message(self, text: str) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="user",
            content=text,
            status="complete",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        dynamic_context = build_dynamic_context_prompt(self._prompt_context(self.runtime_mode))
        self._transcript.append(build_user_message(text=text, dynamic_context=dynamic_context))
        self._advance_dynamic_prompt_state_after_user_turn()
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

    def clear_message_content(self, message_id: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content = ""
        return message

    def add_message_usage(self, message_id: str, usage: MessageUsage) -> SessionMessage:
        message = self.get_message(message_id)
        message.usage.input_tokens += usage.input_tokens
        message.usage.cached_input_tokens += usage.cached_input_tokens
        message.usage.output_tokens += usage.output_tokens
        return message

    def append_trace_thinking(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        entries = message.trace.entries
        if entries and entries[-1].kind == "thinking":
            entries[-1].text += delta
        else:
            entries.append(TraceEntry(kind="thinking", text=delta))
        return message

    def append_trace_text(self, message_id: str, text: str) -> SessionMessage:
        message = self.get_message(message_id)
        if text:
            self._expand_trace_on_first_entry(message)
            message.trace.entries.append(TraceEntry(kind="text", text=text))
        return message

    def append_trace_notice(self, message_id: str, text: str) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        message.trace.entries.append(TraceEntry(kind="notice", text=text))
        return message

    def append_trace_tool_calls(self, message_id: str, tool_calls: list[ToolCall]) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
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
        self._expand_trace_on_first_entry(message)
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
        message.trace.collapsed = True
        message.usage = usage or MessageUsage()
        if message.content.strip():
            self._transcript.append(ConversationMessage.text_message("assistant", message.content))
        return message

    def fail_message(self, message_id: str, error_text: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "error"
        message.content = error_text
        message.trace.collapsed = True
        return message

    def cancel_message(self, message_id: str, notice_text: str = "本轮已取消。") -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "cancelled"
        if not message.content.strip():
            message.content = notice_text
        message.trace.collapsed = True
        return message

    def get_message(self, message_id: str) -> SessionMessage:
        for message in self._state.messages:
            if message.id == message_id:
                return message
        raise KeyError(f"未找到消息：{message_id}")

    def build_request(
        self,
        tools: list[ToolDefinition],
        *,
        allow_tool_calls: bool,
        mode: RuntimeMode | None = None,
        deferred_tool_groups: list[DeferredToolGroup] | None = None,
    ) -> ChatRequest:
        active_mode = mode or self.runtime_mode
        thinking = self._request_thinking()
        filtered_tools = self._filter_tools_for_mode(tools, active_mode) if allow_tool_calls else []
        payload = build_chat_request_payload(
            context=self._prompt_context(active_mode),
            transcript=self.transcript,
            tools=filtered_tools,
            deferred_tool_groups=deferred_tool_groups,
        )
        return ChatRequest(
            model=self._provider_config.model,
            system=payload.system,
            messages=payload.messages,
            tools=payload.tools,
            allow_tool_calls=allow_tool_calls,
            thinking=thinking,
            mode=active_mode,
        )

    def total_usage(self) -> MessageUsage:
        total = MessageUsage()
        for message in self._state.messages:
            total.input_tokens += message.usage.input_tokens
            total.cached_input_tokens += message.usage.cached_input_tokens
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
    def _expand_trace_on_first_entry(message: SessionMessage) -> None:
        if message.role == "assistant" and message.status == "streaming" and not message.trace.entries:
            message.trace.collapsed = False

    def _resolve_plan_file_path(self, plan_file_path: Path | None) -> Path:
        raw_path = plan_file_path or Path("./.lancher/plan.md")
        if not raw_path.is_absolute():
            raw_path = self._cwd / raw_path
        return raw_path.resolve()

    def _prompt_context(self, mode: RuntimeMode) -> "PromptContext":
        return build_prompt_context(
            cwd=self._cwd,
            current_date=self._current_date,
            runtime_mode=mode,
            plan_file_path=self._plan_file_path,
            previous_runtime_mode=self._state.previous_runtime_mode,
            plan_mode_turn_count=self._state.plan_mode_turn_count,
            pending_plan_entry_kind=self._state.pending_plan_entry_kind,
            pending_plan_exit_notice=self._state.pending_plan_exit_notice,
        )

    def _advance_dynamic_prompt_state_after_user_turn(self) -> None:
        if self._state.runtime_mode == "plan":
            self._state.plan_mode_turn_count += 1
            self._state.pending_plan_entry_kind = None
            return

        self._state.pending_plan_exit_notice = False

    @staticmethod
    def _filter_tools_for_mode(tools: list[ToolDefinition], mode: RuntimeMode) -> list[ToolDefinition]:
        return [tool for tool in tools if mode in tool.allowed_modes]

    @staticmethod
    def _new_message_id() -> str:
        return uuid4().hex

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
