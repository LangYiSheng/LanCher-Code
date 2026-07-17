from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

from lancher_code.context_management import AUTOMATIC_FAILURE_LIMIT, EMERGENCY_MARGIN, automatic_threshold
from lancher_code.errors import (
    ContextCompactionError,
    LanCherError,
    ProviderPromptTooLongError,
    ToolCallParseError,
)
from lancher_code.logging_system import get_logger
from lancher_code.models import (
    CancellationToken,
    ChatRequest,
    ContextCompactionResult,
    MessageUsage,
    PermissionRequest,
    PermissionResolution,
    RuntimeMode,
    ToolCall,
    ToolExecutionResult,
    TurnEvent,
)
from lancher_code.providers.base import ChatProvider
from lancher_code.session import SessionController
from lancher_code.tool_call_parser import ToolCallAssembler
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry

logger = get_logger("turn_runner")

MAX_TOOL_LOOPS = 50
DEFAULT_UNKNOWN_TOOL_STREAK_LIMIT = 3
_QUEUE_END = object()


@dataclass(slots=True)
class _ActiveTurn:
    task: asyncio.Task[None]
    queue: asyncio.Queue[TurnEvent | object]
    cancellation_token: CancellationToken
    pending_permissions: dict[str, asyncio.Future[PermissionResolution]] = field(default_factory=dict)


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
        label = _mode_status_label(mode)
        return TurnEvent(kind="mode_changed", mode=mode, progress_message=label)

    def restore_mode_after_plan(self) -> TurnEvent:
        mode = self._session.restore_mode_after_plan()
        return TurnEvent(kind="mode_changed", mode=mode, progress_message=_mode_status_label(mode))

    def resolve_permission_request(self, resolution: PermissionResolution) -> bool:
        active_turn = self._active_turn
        if active_turn is None:
            return False
        future = active_turn.pending_permissions.get(resolution.request_id)
        if future is None or future.done():
            return False
        future.set_result(resolution)
        return True

    def cancel_active_turn(self) -> bool:
        if self._active_turn is None:
            return False
        self._active_turn.cancellation_token.cancel()
        self._active_turn.task.cancel()
        for future in self._active_turn.pending_permissions.values():
            if not future.done():
                future.cancel()
        self._active_turn.pending_permissions.clear()
        return True

    @property
    def has_active_turn(self) -> bool:
        return self._active_turn is not None

    async def compact_context(self) -> ContextCompactionResult:
        if self.has_active_turn:
            raise ContextCompactionError("模型正在响应，暂时不能压缩上下文。")
        visible_tools = self._tool_registry.list_definitions(
            discovered_names=set(),
            mode=self._session.runtime_mode,
        )
        return await self._session.compact_context(
            provider=self._provider,
            visible_tools=visible_tools,
            deferred_tool_groups=self._tool_registry.list_deferred_index(
                mode=self._session.runtime_mode
            ),
            persist=True,
        )

    async def run_user_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        queue: asyncio.Queue[TurnEvent | object] = asyncio.Queue()
        cancellation_token = CancellationToken()
        active_turn = _ActiveTurn(
            task=asyncio.create_task(self._run_turn(text, queue, cancellation_token)),
            queue=queue,
            cancellation_token=cancellation_token,
        )
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
            await asyncio.gather(active_turn.task, return_exceptions=True)

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
        discovered_tool_names: set[str] = set()

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

                visible_tools = self._tool_registry.list_definitions(
                    discovered_names=discovered_tool_names,
                    mode=self._session.runtime_mode,
                )
                await self._session.offload_large_tool_results()
                deferred_tool_groups = self._tool_registry.list_deferred_index(
                    mode=self._session.runtime_mode
                )
                request = self._session.build_request(
                    visible_tools,
                    allow_tool_calls=True,
                    mode=self._session.runtime_mode,
                    deferred_tool_groups=deferred_tool_groups,
                )
                request.cancellation_token = cancellation_token
                context_state = self._session.context_state
                estimated_tokens = self._session.estimate_request_tokens(request)
                if (
                    not context_state.automatic_compaction_disabled
                    and estimated_tokens >= automatic_threshold(self._session.context_window)
                ):
                    await self._emit(
                        queue,
                        TurnEvent(
                            kind="progress_updated",
                            message=self._session.get_message(assistant_message.id),
                            progress_message="正在自动压缩上下文...",
                        ),
                    )
                    try:
                        compaction_result = await self._session.compact_context(
                            provider=self._provider,
                            visible_tools=visible_tools,
                            deferred_tool_groups=deferred_tool_groups,
                            cancellation_token=cancellation_token,
                        )
                    except Exception as exc:
                        context_state.automatic_failure_count += 1
                        if context_state.automatic_failure_count >= AUTOMATIC_FAILURE_LIMIT:
                            context_state.automatic_compaction_disabled = True
                        logger.exception(
                            "event=automatic_context_compaction_failed context_id=%s failure_count=%s",
                            context_state.context_id,
                            context_state.automatic_failure_count,
                        )
                        if estimated_tokens >= self._session.context_window - EMERGENCY_MARGIN:
                            raise ContextCompactionError(f"自动压缩失败：{exc}") from exc
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="progress_updated",
                                message=self._session.get_message(assistant_message.id),
                                progress_message="自动压缩失败，本轮将继续处理",
                            ),
                        )
                    else:
                        context_state.automatic_failure_count = 0
                        context_state.automatic_compaction_disabled = False
                        logger.info(
                            "event=automatic_context_compaction_succeeded context_id=%s before_tokens=%s after_tokens=%s",
                            context_state.context_id,
                            compaction_result.before_tokens,
                            compaction_result.after_tokens,
                        )
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="progress_updated",
                                message=self._session.get_message(assistant_message.id),
                                progress_message=(
                                    "已自动压缩上下文，"
                                    f"token 从 {compaction_result.before_tokens} "
                                    f"降至 {compaction_result.after_tokens}"
                                ),
                            ),
                        )
                        request = self._session.build_request(
                            visible_tools,
                            allow_tool_calls=True,
                            mode=self._session.runtime_mode,
                            deferred_tool_groups=deferred_tool_groups,
                        )
                        request.cancellation_token = cancellation_token

                await self._emit(
                    queue,
                    TurnEvent(
                        kind="progress_updated",
                        message=self._session.get_message(assistant_message.id),
                        progress_message=f"第 {loop_count} 轮：等待模型响应",
                    ),
                )

                emergency_attempted = False
                while True:
                    assembler = ToolCallAssembler()
                    collector = _StreamCollector()
                    try:
                        loop_usage = await self._stream_request(
                            request=request,
                            assembler=assembler,
                            collector=collector,
                            assistant_message_id=assistant_message.id,
                            queue=queue,
                        )
                        self._session.update_context_usage(request, loop_usage)
                        break
                    except ProviderPromptTooLongError as prompt_error:
                        if emergency_attempted:
                            raise
                        emergency_attempted = True
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="progress_updated",
                                message=self._session.get_message(assistant_message.id),
                                progress_message="上下文撞墙，自动压缩中...",
                            ),
                        )
                        await self._session.offload_large_tool_results()
                        try:
                            result = await self._session.compact_context(
                                provider=self._provider,
                                visible_tools=visible_tools,
                                deferred_tool_groups=deferred_tool_groups,
                                cancellation_token=cancellation_token,
                            )
                        except Exception:
                            raise prompt_error
                        if result.after_tokens >= self._session.context_window - EMERGENCY_MARGIN:
                            raise
                        logger.info(
                            "event=emergency_context_compaction_succeeded context_id=%s before_tokens=%s after_tokens=%s dropped_groups=%s",
                            self._session.context_state.context_id,
                            result.before_tokens,
                            result.after_tokens,
                            result.dropped_groups,
                        )
                        await self._emit(
                            queue,
                            TurnEvent(
                                kind="progress_updated",
                                message=self._session.get_message(assistant_message.id),
                                progress_message=(
                                    "紧急压缩完成，"
                                    f"token 从 {result.before_tokens} 降至 {result.after_tokens}"
                                ),
                            ),
                        )
                        request = self._session.build_request(
                            visible_tools,
                            allow_tool_calls=True,
                            mode=self._session.runtime_mode,
                            deferred_tool_groups=deferred_tool_groups,
                        )
                        request.cancellation_token = cancellation_token

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
                total_usage.cached_input_tokens += loop_usage.cached_input_tokens
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
                        permission_resolver=self._request_permission,
                        available_tool_names={tool.name for tool in visible_tools},
                    )
                    for result in results:
                        discovered = result.metadata.get("discovered_tool_names")
                        if isinstance(discovered, list):
                            discovered_tool_names.update(
                                name for name in discovered if isinstance(name, str)
                            )
                        self._session.record_read_file_result(result)
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
            logger.exception("event=turn_failed_unexpected exception_type=%s", type(exc).__name__)
            error_text = f"发生未预期异常: {exc}"
            if assistant_message is not None:
                self._session.append_trace_notice(assistant_message.id, error_text)
                message = self._session.fail_message(assistant_message.id, error_text)
                await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
        finally:
            if self._active_turn is not None:
                self._active_turn.pending_permissions.clear()
            auto_save_error = self._session.auto_save()
            if auto_save_error:
                logger.error("event=session_auto_save_failed error=%s", auto_save_error)
            await queue.put(_QUEUE_END)

    async def _stream_request(
        self,
        *,
        request: ChatRequest,
        assembler: ToolCallAssembler,
        collector: _StreamCollector,
        assistant_message_id: str,
        queue: asyncio.Queue[TurnEvent | object],
    ) -> MessageUsage:
        usage = MessageUsage()
        async for event in self._provider.stream_chat(request):
            if event.kind == "thinking_delta" and event.text:
                self._session.append_trace_thinking(assistant_message_id, event.text)
                await self._emit(
                    queue,
                    TurnEvent(
                        kind="progress_updated",
                        message=self._session.get_message(assistant_message_id),
                        usage=self._current_message_usage(assistant_message_id),
                        progress_message="模型正在思考",
                    ),
                )
            elif event.kind == "text_delta" and event.text:
                collector.append(event.text)
                self._session.append_message_content(assistant_message_id, event.text)
                await self._emit(
                    queue,
                    TurnEvent(
                        kind="assistant_text_delta",
                        message=self._session.get_message(assistant_message_id),
                        usage=self._current_message_usage(assistant_message_id),
                        text=event.text,
                    ),
                )
            elif event.kind == "tool_call_delta" and event.tool_call_chunk:
                assembler.consume(event.tool_call_chunk)
            elif event.kind == "message_end":
                usage = event.usage
        return usage

    async def _request_permission(self, permission_request: PermissionRequest) -> PermissionResolution:
        active_turn = self._active_turn
        if active_turn is None:
            return PermissionResolution(request_id=permission_request.request_id, outcome="deny")

        future: asyncio.Future[PermissionResolution] = asyncio.get_running_loop().create_future()
        active_turn.pending_permissions[permission_request.request_id] = future
        await self._emit(active_turn.queue, TurnEvent(kind="permission_request_created", permission_request=permission_request))
        try:
            resolution = await future
            await self._emit(
                active_turn.queue,
                TurnEvent(kind="permission_request_resolved", permission_resolution=resolution),
            )
            return resolution
        finally:
            active_turn.pending_permissions.pop(permission_request.request_id, None)

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
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
        )

    @staticmethod
    def _raise_if_cancelled(cancellation_token: CancellationToken) -> None:
        if cancellation_token.is_cancelled:
            raise asyncio.CancelledError


def _mode_status_label(mode: RuntimeMode) -> str:
    labels = {
        "default": "已切换到 Default 模式",
        "plan": "已切换到 Plan 模式",
        "acceptEdits": "已切换到 AcceptEdits 模式",
        "bypass": "已切换到 Bypass 模式",
    }
    return labels[mode]
