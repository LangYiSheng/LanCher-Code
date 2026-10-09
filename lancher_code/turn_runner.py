from __future__ import annotations

import asyncio
from contextlib import aclosing
import os
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4

from lancher_code.context_management import AUTOMATIC_FAILURE_LIMIT, EMERGENCY_MARGIN, automatic_threshold
from lancher_code.errors import (
    ConfigError,
    ContextCompactionError,
    LanCherError,
    ProviderPromptTooLongError,
    ToolCallParseError,
)
from lancher_code.logging_system import get_logger, register_sensitive_values
from lancher_code.model_catalog import iter_model_refs, model_display_name, resolve_model
from lancher_code.models import (
    AppConfig,
    CancellationToken,
    ChatRequest,
    ContextCompactionResult,
    MessageUsage,
    PendingInput,
    PermissionPolicy,
    PermissionRequest,
    PermissionResolution,
    ProviderConfig,
    RuntimeMode,
    ToolCall,
    ToolExecutionResult,
    TurnEvent,
    WorkPhase,
)
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.factory import create_provider
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
    task_id: str = field(default_factory=lambda: uuid4().hex)
    accepting_input: bool = True
    pending_permissions: dict[str, asyncio.Future[PermissionResolution]] = field(default_factory=dict)
    assistant_message_id: str | None = None


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
        self._manual_compaction = False
        self._model_config: AppConfig | None = None
        self._provider_factory: Callable[[ProviderConfig], ChatProvider] = create_provider
        self._model_notice = ""

    def configure_models(
        self,
        config: AppConfig,
        provider_factory: Callable[[ProviderConfig], ChatProvider] = create_provider,
    ) -> None:
        """绑定启动时的模型目录，复用应用已经创建的 provider。"""
        self._ensure_model_idle()
        snapshot = deepcopy(config)
        resolved = resolve_model(snapshot)
        self._register_model_secrets(snapshot)
        self._session.set_model(resolved, snapshot.default_model, initial=True)
        self._model_config = snapshot
        self._provider_factory = provider_factory
        self._model_notice = ""

    @property
    def model_config(self) -> AppConfig | None:
        return self._model_config

    def _require_model_config(self) -> AppConfig:
        if self._model_config is None:
            raise ConfigError("尚未配置模型目录。")
        return self._model_config

    @staticmethod
    def _register_model_secrets(config: AppConfig) -> None:
        values: list[str] = []
        for provider in config.providers.values():
            candidates = [provider.api_key, *(model.api_key for model in provider.models.values())]
            for candidate in candidates:
                if isinstance(candidate, str):
                    values.extend((candidate, os.path.expandvars(candidate).strip()))
        register_sensitive_values(values)

    @property
    def model_ref(self) -> str | None:
        return self._session.selected_model_ref

    @property
    def model_notice(self) -> str:
        return self._model_notice

    def _ensure_model_idle(self) -> None:
        if self.has_active_turn or self._manual_compaction:
            raise ConfigError("模型正在响应或压缩上下文，请等待完成后再切换模型。")

    def _prepare_model(self, config: AppConfig, model_ref: str) -> tuple[ProviderConfig, ChatProvider]:
        resolved = resolve_model(config, model_ref)
        # 工厂创建失败时也可能记录错误，必须提前注册已经解析的密钥。
        register_sensitive_values([resolved.api_key])
        return resolved, self._provider_factory(resolved)

    def switch_model(self, model_ref: str) -> None:
        self._ensure_model_idle()
        resolved, provider = self._prepare_model(self._require_model_config(), model_ref)
        self._session.set_model(resolved, model_ref)
        self._provider = provider
        self._model_notice = ""

    def reload_models(self, config: AppConfig) -> bool:
        """热更新目录；修改默认值不会覆盖会话中已经选择的模型。"""
        self._ensure_model_idle()
        snapshot = deepcopy(config)
        self._register_model_secrets(snapshot)
        resolve_model(snapshot)
        fallback = self.model_ref not in iter_model_refs(snapshot)
        target = snapshot.default_model if fallback else self.model_ref
        assert target is not None
        resolved = resolve_model(snapshot, target)
        if target != self.model_ref or resolved != self._session.provider_config:
            resolved, provider = self._prepare_model(snapshot, target)
            self._session.set_model(resolved, target)
            self._provider = provider
        self._model_config = snapshot
        self._model_notice = (
            f"当前模型已被删除，已切换到默认模型：{model_display_name(snapshot, target)}。"
            if fallback else ""
        )
        return fallback

    def resume_session(self, name: str, *, force: bool = False) -> int:
        self._ensure_model_idle()
        saved_ref = self._session.read_session_model_ref(name, force=force)
        config = self._require_model_config()
        target = saved_ref if saved_ref in iter_model_refs(config) else config.default_model
        resolved, provider = self._prepare_model(config, target)
        permission_count = self._session.resume_session(
            name, force=force, resolved_model=(resolved, target)
        )
        self._provider = provider
        if saved_ref is None:
            self._model_notice = f"旧会话未记录模型，已使用默认模型：{model_display_name(config, target)}。"
        elif saved_ref != target:
            self._model_notice = f"会话原模型已不存在，已使用默认模型：{model_display_name(config, target)}。"
        else:
            self._model_notice = ""
        return permission_count

    def set_mode(self, mode: RuntimeMode) -> TurnEvent:
        """旧命令入口：阶段和权限不再相互覆盖。"""
        if mode == "plan":
            return self.set_phase("plan")
        return self.set_permission_policy(mode)

    def set_phase(self, phase: WorkPhase) -> TurnEvent:
        self._ensure_model_idle()
        self._session.set_work_phase(phase)
        label = {"discuss": "讨论", "plan": "计划", "execute": "执行"}[phase]
        return TurnEvent(kind="phase_changed", work_phase=phase, progress_message=f"已切换到{label}阶段，权限策略不变")

    def set_permission_policy(self, policy: PermissionPolicy) -> TurnEvent:
        self._ensure_model_idle()
        self._session.set_permission_policy(policy)
        return TurnEvent(kind="policy_changed", permission_policy=policy, progress_message="权限策略已更新，工作阶段不变")

    def restore_mode_after_plan(self) -> TurnEvent:
        return self.set_phase("execute")

    @property
    def pending_inputs(self) -> list[PendingInput]:
        return deepcopy(self._session.pending_inputs)

    @property
    def queue_paused(self) -> bool:
        return any(item.state == "paused" for item in self.pending_inputs)

    def _pending_changed(self, items: list[PendingInput], item_id: str | None = None) -> None:
        self._session.update_pending_inputs(items)
        if self._active_turn is not None:
            self._active_turn.queue.put_nowait(TurnEvent(kind="pending_input_changed", pending_input_id=item_id))

    def enqueue_input(self, text: str, delivery: str = "follow_up") -> PendingInput:
        text = text.strip()
        if not text or text.startswith("/"):
            raise ConfigError("待处理消息不能为空；斜杠命令请在当前任务结束后执行。")
        if delivery not in {"follow_up", "steer"}:
            raise ConfigError("未知的消息发送方式。")
        active = self._active_turn
        can_steer = active is not None and active.accepting_input and not active.cancellation_token.is_cancelled
        item = PendingInput(
            id=uuid4().hex, text=text, delivery=delivery,
            target_task_id=active.task_id if delivery == "steer" and can_steer else None,
            state=("pending" if can_steer else "paused") if delivery == "steer"
                else ("paused" if self.queue_paused else "pending"),
        )
        self._pending_changed([*self.pending_inputs, item], item.id)
        if delivery == "steer" and can_steer:
            self._supersede_permissions()
        return deepcopy(item)

    def update_pending_input(self, item_id: str, text: str) -> PendingInput:
        if not text.strip() or text.lstrip().startswith("/"):
            raise ConfigError("待处理消息不能为空，也不能是斜杠命令。")
        items = self.pending_inputs
        item = next((entry for entry in items if entry.id == item_id), None)
        if item is None:
            raise ConfigError("这条消息已经生效或被移除，请刷新待处理列表。")
        item.text = text.strip()
        self._pending_changed(items, item_id)
        return deepcopy(item)

    def remove_pending_input(self, item_id: str) -> None:
        items = self.pending_inputs
        if not any(item.id == item_id for item in items):
            raise ConfigError("这条消息已经生效或被移除。")
        self._pending_changed([item for item in items if item.id != item_id], item_id)

    def convert_pending_input(self, item_id: str, delivery: str) -> PendingInput:
        if delivery not in {"follow_up", "steer"}:
            raise ConfigError("未知的消息发送方式。")
        items = self.pending_inputs
        item = next((entry for entry in items if entry.id == item_id), None)
        if item is None:
            raise ConfigError("这条消息已经生效或被移除。")
        active = self._active_turn
        can_steer = active is not None and active.accepting_input and not active.cancellation_token.is_cancelled
        item.delivery = delivery
        item.target_task_id = active.task_id if delivery == "steer" and can_steer else None
        if delivery == "steer":
            item.state = "pending" if can_steer else "paused"
        self._pending_changed(items, item_id)
        if delivery == "steer" and can_steer and item.state == "pending":
            self._supersede_permissions()
        return deepcopy(item)

    def pause_queue(self) -> None:
        items = self.pending_inputs
        if not items or all(item.state == "paused" for item in items):
            return
        for item in items:
            item.state = "paused"
        self._pending_changed(items)

    def resume_queue(self) -> None:
        items = self.pending_inputs
        if not items:
            return
        for item in items:
            item.state = "pending"
            if self._active_turn is None or item.target_task_id != self._active_turn.task_id:
                item.delivery = "follow_up"
                item.target_task_id = None
        self._pending_changed(items)
        if self._has_steering():
            self._supersede_permissions()

    def _has_steering(self) -> bool:
        active = self._active_turn
        return bool(active and any(item.delivery == "steer" and item.state == "pending"
            and item.target_task_id == active.task_id for item in self._session.pending_inputs))

    def _supersede_permissions(self) -> None:
        if self._active_turn is not None:
            for request_id, future in self._active_turn.pending_permissions.items():
                if not future.done():
                    future.set_result(PermissionResolution(request_id=request_id, outcome="superseded"))

    async def _apply_steering(self, message_id: str, usage: MessageUsage, queue: asyncio.Queue) -> str | None:
        if not self._has_steering():
            return None
        active = self._active_turn
        assert active is not None
        items = self.pending_inputs
        selected = [item for item in items if item.delivery == "steer" and item.state == "pending"
                    and item.target_task_id == active.task_id]
        selected_ids = {item.id for item in selected}
        self._pending_changed([item for item in items if item.id not in selected_ids])
        message = self._session.complete_message(message_id, usage)
        await self._emit(queue, TurnEvent(kind="assistant_message_completed", message=message, usage=usage))
        for item in selected:
            user = self._session.create_user_message(item.text)
            await self._emit(queue, TurnEvent(kind="user_message_created", message=user))
            await self._emit(queue, TurnEvent(kind="steering_applied", pending_input_id=item.id, text=item.text))
        assistant = self._session.create_assistant_message()
        active.assistant_message_id = assistant.id
        await self._emit(queue, TurnEvent(kind="assistant_message_started", message=assistant))
        return assistant.id

    async def run_next_queued_turn(self) -> AsyncIterator[TurnEvent]:
        self._ensure_model_idle()
        items = self.pending_inputs
        if self.queue_paused or not items or items[0].state != "pending" or items[0].delivery != "follow_up":
            return
        item = items.pop(0)
        self._pending_changed(items, item.id)
        async with aclosing(self.run_user_turn(item.text)) as events:
            async for event in events:
                yield event

    def prepare_plan_execution(self, session_id: str, digest: str) -> str:
        self._ensure_model_idle()
        snapshot = self._session.plan_snapshot
        if session_id != self._session.session_id or snapshot is None or not snapshot.ready or snapshot.digest != digest:
            raise ConfigError("计划已改变或尚未完成，请查看当前会话的最新计划后再开始。")
        content = snapshot.content
        snapshot.ready = False
        self._session.set_plan_snapshot(snapshot)
        self._session.set_work_phase("execute")
        return f"按我确认的以下计划开始执行。\n计划版本：{digest}\n\n{content}"

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
        self._active_turn.accepting_input = False
        self.pause_queue()
        self._active_turn.cancellation_token.cancel()
        if not self._active_turn.task.cancelling():
            self._active_turn.task.cancel()
        for future in self._active_turn.pending_permissions.values():
            if not future.done():
                future.cancel()
        self._active_turn.pending_permissions.clear()
        return True

    @property
    def has_active_turn(self) -> bool:
        return self._active_turn is not None

    async def stop_and_wait(self) -> None:
        """界面关闭前等待当前任务收尾，不能把资源回收留给事件循环析构。"""
        active = self._active_turn
        if active is not None:
            self.cancel_active_turn()
            await asyncio.gather(active.task, return_exceptions=True)
            if self._active_turn is active:
                self._active_turn = None

    async def compact_context(self) -> ContextCompactionResult:
        if self.has_active_turn or self._manual_compaction:
            raise ContextCompactionError("模型正在响应，暂时不能压缩上下文。")
        self._manual_compaction = True
        try:
            visible_tools = self._tool_registry.list_definitions(
                discovered_names=set(),
                mode=self._session.runtime_mode,
                work_phase=self._session.work_phase,
            )
            return await self._session.compact_context(
                provider=self._provider,
                visible_tools=visible_tools,
                deferred_tool_groups=self._tool_registry.list_deferred_index(
                    mode=self._session.runtime_mode,
                    work_phase=self._session.work_phase,
                ),
                persist=True,
            )
        finally:
            self._manual_compaction = False

    async def run_user_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        self._ensure_model_idle()
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
            if not active_turn.task.done():
                # 消费者退出也必须收拢后台任务，不能遗留仍在等待批准的执行器。
                active_turn.cancellation_token.cancel()
                # 重复停止不能打断工具正在进行的子进程及管道清理。
                if not active_turn.task.cancelling():
                    active_turn.task.cancel()
                for future in active_turn.pending_permissions.values():
                    if not future.done():
                        future.cancel()
                self.pause_queue()
            try:
                await asyncio.gather(active_turn.task, return_exceptions=True)
            finally:
                if self._active_turn is active_turn:
                    self._active_turn = None

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
        pending_tool_calls: list[ToolCall] = []
        phase = self._session.work_phase
        policy = self._session.permission_policy
        completed = False
        written_plan_digest: str | None = None

        try:
            user_message = self._session.create_user_message(text)
            await self._emit(queue, TurnEvent(kind="user_message_created", message=user_message))

            assistant_message = self._session.create_assistant_message()
            if self._active_turn is not None:
                self._active_turn.assistant_message_id = assistant_message.id
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
                    message = self._session.fail_message(assistant_message.id, error_text)
                    await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
                    return

                visible_tools = self._tool_registry.list_definitions(
                    discovered_names=discovered_tool_names,
                    mode=self._session.runtime_mode,
                    work_phase=phase,
                )
                await self._session.offload_large_tool_results()
                deferred_tool_groups = self._tool_registry.list_deferred_index(
                    mode=self._session.runtime_mode,
                    work_phase=phase,
                )
                request = self._session.build_request(
                    visible_tools,
                    allow_tool_calls=True,
                    mode=self._session.runtime_mode,
                    work_phase=phase,
                    permission_policy=policy,
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
                            work_phase=phase,
                            permission_policy=policy,
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
                            work_phase=phase,
                            permission_policy=policy,
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
                        self._session.clear_message_content(assistant_message.id)

                    self._session.append_assistant_tool_calls(tool_calls)
                    pending_tool_calls = list(tool_calls)
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

                    recorded_ids: set[str] = set()

                    async def report_started(call: ToolCall) -> None:
                        message = self._session.set_trace_tool_state(assistant_message.id, call.call_id, "running")
                        await self._emit(queue, TurnEvent(kind="progress_updated", message=message,
                            tool_call=call, progress_message=f"正在执行 {call.tool_name}"))

                    async def report_result(result: ToolExecutionResult) -> None:
                        # 回调与返回列表共用幂等入口，取消只补齐尚未收到的结果。
                        if result.call_id in recorded_ids:
                            return
                        recorded_ids.add(result.call_id)
                        self._session.append_tool_results([result])
                        pending_tool_calls[:] = [call for call in pending_tool_calls if call.call_id != result.call_id]
                        message = self._session.append_trace_tool_results(assistant_message.id, [result])
                        await self._emit(queue, TurnEvent(kind="tool_result_received", message=message,
                            usage=self._current_message_usage(assistant_message.id), tool_result=result))

                    results = precomputed_results or await self._tool_executor.execute_calls(
                        tool_calls,
                        mode=self._session.runtime_mode,
                        work_phase=phase,
                        permission_policy=policy,
                        plan_file_path=self._session.plan_file_path,
                        cancellation_token=cancellation_token,
                        permission_resolver=self._request_permission,
                        available_tool_names={tool.name for tool in visible_tools},
                        should_interrupt=self._has_steering,
                        on_call_started=report_started,
                        on_result=report_result,
                    )
                    calls_by_id = {call.call_id: call for call in tool_calls}
                    for result in results:
                        await report_result(result)
                        discovered = result.metadata.get("discovered_tool_names")
                        if isinstance(discovered, list):
                            discovered_tool_names.update(
                                name for name in discovered if isinstance(name, str)
                            )
                        self._session.record_read_file_result(result)
                        if phase == "plan" and result.ok and result.tool_name == "write_plan_file":
                            plan_content = calls_by_id[result.call_id].arguments.get("content")
                            if isinstance(plan_content, str):
                                snapshot = self._session.set_plan_snapshot(plan_content, source_message_id=assistant_message.id)
                                written_plan_digest = snapshot.digest if snapshot else None
                    next_message_id = await self._apply_steering(assistant_message.id, total_usage, queue)
                    if next_message_id is not None:
                        assistant_message = self._session.get_message(next_message_id)
                        total_usage = MessageUsage()
                        unknown_tool_streak = 0
                        written_plan_digest = None
                        continue

                    unknown_tool_streak = self._next_unknown_tool_streak(unknown_tool_streak, results)
                    if unknown_tool_streak >= self._unknown_tool_streak_limit:
                        error_text = (
                            f"连续请求未知工具已达到 {self._unknown_tool_streak_limit} 次，"
                            "为避免无效循环，本轮已停止。"
                        )
                        message = self._session.fail_message(assistant_message.id, error_text)
                        await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
                        return
                    continue

                unknown_tool_streak = 0
                next_message_id = await self._apply_steering(assistant_message.id, total_usage, queue)
                if next_message_id is not None:
                    assistant_message = self._session.get_message(next_message_id)
                    total_usage = MessageUsage()
                    written_plan_digest = None
                    continue
                # 最终检查与关闭接收在同一事件循环片段完成；晚到的补充保留为暂停消息。
                if self._active_turn is not None:
                    self._active_turn.accepting_input = False
                message = self._session.complete_message(assistant_message.id, total_usage)
                snapshot = self._session.plan_snapshot
                if phase == "plan" and snapshot is not None and snapshot.digest == written_plan_digest:
                    snapshot.ready = True
                    self._session.set_plan_snapshot(snapshot)
                completed = True
                await self._emit(
                    queue,
                    TurnEvent(kind="assistant_message_completed", message=message, usage=total_usage),
                )
                await self._emit(queue, TurnEvent(kind="turn_completed", message=message,
                    task_id=self._active_turn.task_id if self._active_turn else None))
                return
        except asyncio.CancelledError:
            cancellation_token.cancel()
            if assistant_message is not None:
                self._close_pending_tool_calls(assistant_message.id, pending_tool_calls, "本轮已取消")
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
                self._close_pending_tool_calls(assistant_message.id, pending_tool_calls, "本轮异常中断")
                message = self._session.fail_message(assistant_message.id, exc.user_message)
                await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=exc.user_message))
        except Exception as exc:
            logger.exception("event=turn_failed_unexpected exception_type=%s", type(exc).__name__)
            error_text = f"发生未预期异常: {exc}"
            if assistant_message is not None:
                self._close_pending_tool_calls(assistant_message.id, pending_tool_calls, "本轮异常中断")
                message = self._session.fail_message(assistant_message.id, error_text)
                await self._emit(queue, TurnEvent(kind="turn_failed", message=message, error_text=error_text))
        finally:
            if self._active_turn is not None:
                self._active_turn.accepting_input = False
                self._active_turn.pending_permissions.clear()
            if not completed:
                self.pause_queue()
            auto_save_error = self._session.auto_save()
            if auto_save_error:
                logger.error("event=session_auto_save_failed error=%s", auto_save_error)
            await queue.put(_QUEUE_END)

    def _close_pending_tool_calls(self, message_id: str, pending: list[ToolCall], reason: str) -> None:
        if not pending:
            return
        # 仅补齐尚未记录结果的调用，不能把中断误报为工具完全没有执行。
        message = f"{reason}，未获得此工具调用的完整结果。操作可能已部分执行，请先检查当前状态，勿直接重复执行。"
        results = [
            ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                content=message,
                is_error=True,
                summary="工具结果未完成",
                error_code="tool_result_interrupted",
                error_message=message,
            )
            for call in pending
        ]
        self._session.append_tool_results(results)
        pending.clear()
        self._session.append_trace_tool_results(message_id, results)

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
                message = self._session.get_message(assistant_message_id)
                entries = message.trace.entries
                if entries and entries[-1].kind in {"thinking", "text"} and entries[-1].metadata.get("state") == "streaming":
                    self._session.finish_trace_segment(assistant_message_id)
                    await self._emit(queue, TurnEvent(
                        kind="progress_updated", message=message,
                        usage=self._current_message_usage(assistant_message_id),
                        progress_message="正在准备工具调用",
                    ))
                assembler.consume(event.tool_call_chunk)
            elif event.kind == "message_end":
                self._session.finish_trace_segment(assistant_message_id)
                usage = event.usage
        self._session.finish_trace_segment(assistant_message_id)
        return usage

    async def _request_permission(self, permission_request: PermissionRequest) -> PermissionResolution:
        active_turn = self._active_turn
        if active_turn is None:
            return PermissionResolution(request_id=permission_request.request_id, outcome="deny")
        if self._has_steering():
            return PermissionResolution(request_id=permission_request.request_id, outcome="superseded")

        future: asyncio.Future[PermissionResolution] = asyncio.get_running_loop().create_future()
        active_turn.pending_permissions[permission_request.request_id] = future
        message = (self._session.set_trace_tool_state(active_turn.assistant_message_id, permission_request.call_id, "awaiting_permission")
                   if active_turn.assistant_message_id else None)
        await self._emit(active_turn.queue, TurnEvent(kind="permission_request_created", message=message,
            permission_request=permission_request))
        try:
            resolution = await future
            if resolution.outcome != "superseded":
                await self._emit(
                    active_turn.queue,
                    TurnEvent(kind="permission_request_resolved", message=message, permission_resolution=resolution),
                )
            return resolution
        finally:
            active_turn.pending_permissions.pop(permission_request.request_id, None)
            await self._emit(active_turn.queue, TurnEvent(kind="permission_request_closed", message=message,
                permission_request=permission_request))

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
