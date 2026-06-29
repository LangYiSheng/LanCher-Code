from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from lancher_code.errors import LanCherError, ToolCallParseError
from lancher_code.models import CancellationToken, MessageUsage, RuntimeMode, ToolCall, ToolExecutionResult, TurnEvent
from lancher_code.providers.base import ChatProvider
from lancher_code.session import SessionController
from lancher_code.tool_call_parser import ToolCallAssembler
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry

MAX_TOOL_LOOPS = 50
DEFAULT_UNKNOWN_TOOL_STREAK_LIMIT = 3
_QUEUE_END = object()


@dataclass(slots=True)
class _ActiveTurn:
    task: asyncio.Task[None]
    queue: asyncio.Queue[TurnEvent | object]
    cancellation_token: CancellationToken


class _StreamCollector:
    def __init__(self) -> None:
        self._text_parts: list[str] = []

    def append(self, delta: str) -> None:
        self._text_parts.append(delta)

    @property
    def text(self) -> str:
        return "".join(self._text_parts)


class TurnRunner:
    def __init__(
        self,
        provider: ChatProvider,
        session_controller: SessionController,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        *,
        max_tool_loops: int = MAX_TOOL_LOOPS,
        unknown_tool_streak_limit: int = DEFAULT_UNKNOWN_TOOL_STREAK_LIMIT,
    ) -> None:
        self._provider = provider
        self._session = session_controller
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._max_tool_loops = max_tool_loops
        self._unknown_tool_streak_limit = unknown_tool_streak_limit
        self._active_turn: _ActiveTurn | None = None

    def set_mode(self, mode: RuntimeMode) -> TurnEvent:
        self._session.set_runtime_mode(mode)
        label = "Plan Mode 已激活" if mode == "plan" else "已恢复 Normal Mode"
        return TurnEvent(kind="mode_changed", mode=mode, progress_message=label)

    def cancel_active_turn(self) -> bool:
        if self._active_turn is None:
            return False
        self._active_turn.cancellation_token.cancel()
        self._active_turn.task.cancel()
        return True

    async def run_user_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        queue: asyncio.Queue[TurnEvent | object] = asyncio.Queue()
        cancellation_token = CancellationToken()
        task = asyncio.create_task(self._run_turn(text, queue, cancellation_token))
        active_turn = _ActiveTurn(task=task, queue=queue, cancellation_token=cancellation_token)
        self._active_turn = active_turn

        try:
            while True:
                item = await queue.get()
                if item is _QUEUE_END:
                    break
                assert isinstance(item, TurnEvent)
                yield item
        finally:
            if self._active_turn is active_turn:
                self._active_turn = None
            await asyncio.gather(task, return_exceptions=True)

    async def _run_turn(
        self,
        text: str,
        queue: asyncio.Queue[TurnEvent | object],
        cancellation_token: CancellationToken,
    ) -> None:
        assistant_message = None
        total_usage = MessageUsage()
        loop_count = 0
        unknown_tool_streak = 0

        try:
            user_message = self._session.create_user_message(text)
            await self._emit(queue, TurnEvent(kind="user_message_created", message=user_message))

            assistant_message = self._session.create_assistant_message()
            await self._emit(queue, TurnEvent(kind="assistant_message_started", message=assistant_message))
            await self._emit(
                queue,
                TurnEvent(
                    kind="progress_updated",
                    message=assistant_message,
                    progress_message="开始处理本轮请求",
                ),
            )

            while True:
                loop_count += 1
                self._raise_if_cancelled(cancellation_token)
                if loop_count > self._max_tool_loops:
                    error_text = f"本轮工具循环达到上限（{self._max_tool_loops} 次），已停止继续执行。"
                    self._session.append_trace_notice(assistant_message.id, error_text)
                    message = self._session.fail_message(assistant_message.id, error_text)
                    await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
                    return

                request = self._session.build_request(
                    self._tool_registry.list_definitions(mode=self._session.runtime_mode),
                    allow_tool_calls=True,
                    mode=self._session.runtime_mode,
                )
                request.cancellation_token = cancellation_token
                loop_usage = MessageUsage()
                assembler = ToolCallAssembler()
                collector = _StreamCollector()

                await self._emit(
                    queue,
                    TurnEvent(
                        kind="progress_updated",
                        message=self._session.get_message(assistant_message.id),
                        progress_message=f"第 {loop_count} 轮：等待模型响应",
                    ),
                )

                async for event in self._provider.stream_chat(request):
                    if event.kind == "thinking_delta" and event.text:
                        self._session.append_trace_thinking(assistant_message.id, event.text)
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="progress_updated",
                                message=self._session.get_message(assistant_message.id),
                                usage=self._current_message_usage(assistant_message.id),
                                progress_message="模型正在思考",
                            ),
                        )
                    elif event.kind == "text_delta" and event.text:
                        collector.append(event.text)
                        self._session.append_message_content(assistant_message.id, event.text)
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="assistant_text_delta",
                                message=self._session.get_message(assistant_message.id),
                                usage=self._current_message_usage(assistant_message.id),
                                text=event.text,
                            ),
                        )
                    elif event.kind == "tool_call_delta" and event.tool_call_chunk:
                        assembler.consume(event.tool_call_chunk)
                    elif event.kind == "message_end":
                        loop_usage = event.usage

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
                await self._emit(
                    queue,
                    TurnEvent(
                        kind="usage_updated",
                        message=self._session.get_message(assistant_message.id),
                        usage=self._current_message_usage(assistant_message.id),
                    ),
                )

                if tool_calls:
                    if collector.text:
                        self._session.append_trace_text(assistant_message.id, collector.text)
                        self._session.clear_message_content(assistant_message.id)

                    self._session.append_assistant_tool_calls(tool_calls)
                    self._session.append_trace_tool_calls(assistant_message.id, tool_calls)
                    for call in tool_calls:
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="tool_call_started",
                                message=self._session.get_message(assistant_message.id),
                                usage=self._current_message_usage(assistant_message.id),
                                tool_call=call,
                            ),
                        )

                    await self._emit(
                        queue,
                        TurnEvent(
                            kind="progress_updated",
                            message=self._session.get_message(assistant_message.id),
                            usage=self._current_message_usage(assistant_message.id),
                            progress_message=f"第 {loop_count} 轮：执行 {len(tool_calls)} 个工具调用",
                        ),
                    )

                    results = precomputed_results or await self._tool_executor.execute_calls(
                        tool_calls,
                        mode=self._session.runtime_mode,
                        plan_file_path=self._session.plan_file_path,
                        cancellation_token=cancellation_token,
                    )
                    self._session.append_tool_results(results)
                    self._session.append_trace_tool_results(assistant_message.id, results)
                    for result in results:
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="tool_result_received",
                                message=self._session.get_message(assistant_message.id),
                                usage=self._current_message_usage(assistant_message.id),
                                tool_result=result,
                            ),
                        )

                    unknown_tool_streak = self._next_unknown_tool_streak(unknown_tool_streak, results)
                    if unknown_tool_streak >= self._unknown_tool_streak_limit:
                        error_text = (
                            f"连续请求未知工具已达到 {self._unknown_tool_streak_limit} 次，"
                            "为避免无效循环，本轮已停止。"
                        )
                        self._session.append_trace_notice(assistant_message.id, error_text)
                        message = self._session.fail_message(assistant_message.id, error_text)
                        await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
                        return
                    continue

                unknown_tool_streak = 0
                message = self._session.complete_message(assistant_message.id, total_usage)
                await self._emit(
                    queue,
                    TurnEvent(kind="assistant_message_completed", message=message, usage=total_usage),
                )
                return
        except asyncio.CancelledError:
            cancellation_token.cancel()
            if assistant_message is not None:
                self._session.append_trace_notice(assistant_message.id, "本轮已取消。")
                message = self._session.cancel_message(assistant_message.id)
                await self._emit(
                    queue,
                    TurnEvent(
                        kind="turn_cancelled",
                        message=message,
                        usage=self._current_message_usage(assistant_message.id),
                        progress_message="本轮已取消",
                    ),
                )
            return
        except LanCherError as exc:
            if assistant_message is not None:
                self._session.append_trace_notice(assistant_message.id, exc.user_message)
                message = self._session.fail_message(assistant_message.id, exc.user_message)
                await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=exc.user_message))
        except Exception as exc:
            error_text = f"发生未预期异常: {exc}"
            if assistant_message is not None:
                self._session.append_trace_notice(assistant_message.id, error_text)
                message = self._session.fail_message(assistant_message.id, error_text)
                await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
        finally:
            await queue.put(_QUEUE_END)

    @staticmethod
    async def _emit(queue: asyncio.Queue[TurnEvent | object], event: TurnEvent) -> None:
        await queue.put(event)

    @staticmethod
    def _synthetic_tool_call() -> ToolCall:
        return ToolCall(
            call_index=0,
            call_id=f"tool-call-parse-{uuid4().hex[:8]}",
            tool_name="tool_call_parser",
            arguments={},
            arguments_json="{}",
        )

    @staticmethod
    def _next_unknown_tool_streak(current_streak: int, results: list[ToolExecutionResult]) -> int:
        if not results:
            return 0
        streak = current_streak
        for result in results:
            if result.error_code == "tool_not_found":
                streak += 1
            else:
                streak = 0
        return streak

    def _current_message_usage(self, message_id: str) -> MessageUsage:
        usage = self._session.get_message(message_id).usage
        return MessageUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    @staticmethod
    def _raise_if_cancelled(cancellation_token: CancellationToken) -> None:
        if cancellation_token.is_cancelled:
            raise asyncio.CancelledError
