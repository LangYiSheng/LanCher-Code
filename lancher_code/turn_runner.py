from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from lancher_code.errors import LanCherError, ToolCallParseError
from lancher_code.models import MessageUsage, ToolCall, ToolExecutionResult, TurnEvent
from lancher_code.providers.base import ChatProvider
from lancher_code.session import SessionController
from lancher_code.tool_call_parser import ToolCallAssembler
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry

MAX_TOOL_LOOPS = 20


class TurnRunner:
    def __init__(
        self,
        provider: ChatProvider,
        session_controller: SessionController,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
    ) -> None:
        self._provider = provider
        self._session = session_controller
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor

    async def run_user_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        user_message = self._session.create_user_message(text)
        yield TurnEvent(kind="user_message_created", message=user_message)

        assistant_message = self._session.create_assistant_message()
        yield TurnEvent(kind="assistant_message_started", message=assistant_message)

        total_usage = MessageUsage()
        loop_count = 0

        try:
            while True:
                loop_count += 1
                if loop_count > MAX_TOOL_LOOPS:
                    error_text = f"本轮工具循环达到上限（{MAX_TOOL_LOOPS} 次），已停止继续执行。"
                    self._session.append_trace_notice(assistant_message.id, error_text)
                    message = self._session.fail_message(assistant_message.id, error_text)
                    yield TurnEvent(kind="turn_failed", message=message, error_text=error_text)
                    return

                request = self._session.build_request(
                    self._tool_registry.list_definitions(),
                    allow_tool_calls=True,
                )
                loop_usage = MessageUsage()
                assembler = ToolCallAssembler()
                buffered_text_parts: list[str] = []

                async for event in self._provider.stream_chat(request):
                    if event.kind == "thinking_delta" and event.text:
                        self._session.append_trace_thinking(assistant_message.id, event.text)
                        yield self._trace_updated_event(assistant_message.id)
                    elif event.kind == "text_delta" and event.text:
                        buffered_text_parts.append(event.text)
                    elif event.kind == "tool_call_delta" and event.tool_call_chunk:
                        assembler.consume(event.tool_call_chunk)
                    elif event.kind == "message_end":
                        loop_usage = event.usage

                buffered_text = "".join(buffered_text_parts)
                try:
                    tool_calls = assembler.finalize()
                    precomputed_results: list[ToolExecutionResult] = []
                except ToolCallParseError as exc:
                    tool_calls = [self._synthetic_tool_call()]
                    precomputed_results = [
                        ToolExecutionResult(
                            call_id=tool_calls[0].call_id,
                            tool_name=tool_calls[0].tool_name,
                            content=exc.user_message,
                            is_error=True,
                            metadata={},
                            summary="工具调用解析失败",
                            error_code="tool_call_parse_error",
                            error_message=exc.user_message,
                        )
                    ]

                total_usage.input_tokens += loop_usage.input_tokens
                total_usage.output_tokens += loop_usage.output_tokens
                self._session.add_message_usage(assistant_message.id, loop_usage)
                yield self._trace_updated_event(assistant_message.id)

                if tool_calls:
                    if buffered_text:
                        self._session.append_trace_text(assistant_message.id, buffered_text)
                        yield self._trace_updated_event(assistant_message.id)

                    self._session.append_assistant_tool_calls(tool_calls)
                    self._session.append_trace_tool_calls(assistant_message.id, tool_calls)
                    yield self._trace_updated_event(assistant_message.id)

                    results = precomputed_results or await self._tool_executor.execute_calls(tool_calls)
                    self._session.append_tool_results(results)
                    self._session.append_trace_tool_results(assistant_message.id, results)
                    yield self._trace_updated_event(assistant_message.id)
                    continue

                if buffered_text:
                    self._session.append_message_content(assistant_message.id, buffered_text)
                message = self._session.complete_message(assistant_message.id, total_usage)
                yield TurnEvent(kind="assistant_message_completed", message=message, usage=total_usage)
                return
        except LanCherError as exc:
            self._session.append_trace_notice(assistant_message.id, exc.user_message)
            message = self._session.fail_message(assistant_message.id, exc.user_message)
            yield TurnEvent(kind="turn_failed", message=message, error_text=exc.user_message)
        except Exception as exc:
            error_text = f"发生未预期异常: {exc}"
            self._session.append_trace_notice(assistant_message.id, error_text)
            message = self._session.fail_message(assistant_message.id, error_text)
            yield TurnEvent(kind="turn_failed", message=message, error_text=error_text)

    @staticmethod
    def _synthetic_tool_call() -> ToolCall:
        return ToolCall(
            call_index=0,
            call_id=f"tool-call-parse-{uuid4().hex[:8]}",
            tool_name="tool_call_parser",
            arguments={},
            arguments_json="{}",
        )

    def _trace_updated_event(self, message_id: str) -> TurnEvent:
        return TurnEvent(
            kind="assistant_trace_updated",
            message=self._session.get_message(message_id),
            usage=self._current_message_usage(message_id),
        )

    def _current_message_usage(self, message_id: str) -> MessageUsage:
        usage = self._session.get_message(message_id).usage
        return MessageUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
