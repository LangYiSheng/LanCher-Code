from __future__ import annotations

import asyncio
from contextlib import aclosing
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from uuid import uuid4

from lancher_code.agent.events import TurnEvent
from lancher_code.agent.inputs import PendingInputQueue
from lancher_code.agent.selection import ModelSelection
from lancher_code.agent.streaming import collect_response
from lancher_code.agent.tool_batch import (
    close_pending_tool_calls,
    execute_batch,
    next_unknown_tool_streak,
)
from lancher_code.context.compaction import AUTOMATIC_FAILURE_LIMIT
from lancher_code.context.budget import context_budget
from lancher_code.errors import (
    ConfigError,
    ContextCompactionError,
    LanCherError,
    ProviderPromptTooLongError,
)
from lancher_code.logging_system import get_logger
from lancher_code.config.models import AppConfig
from lancher_code.contracts.control import CancellationToken, PermissionPolicy, WorkPhase
from lancher_code.context.models import CompactionActivity, CompactionTrigger, ContextCompactionResult
from lancher_code.usage.models import MessageUsage
from lancher_code.sessions.models import PendingInput
from lancher_code.permissions.models import PermissionRequest, PermissionResolution
from lancher_code.providers.models import ProviderConfig
from lancher_code.contracts.tools import ToolCall
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.factory import create_provider
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.storage import SessionRepositoryError
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
        self._models = ModelSelection(provider, session_controller)
        self._session = session_controller
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._execution_runtime = tool_executor.execution_runtime
        self._session.bind_execution_runtime(self._execution_runtime)
        self._max_tool_loops = max_tool_loops
        self._unknown_tool_streak_limit = unknown_tool_streak_limit
        self._active_turn: _ActiveTurn | None = None
        self._manual_compaction = False
        self._compaction_task: asyncio.Task | None = None
        self._inputs = PendingInputQueue(
            read=lambda: self._session.pending_inputs, write=self._pending_changed,
            active=self._input_target, on_steer=self._supersede_permissions,
        )

    def _input_target(self) -> tuple[str | None, bool]:
        active = self._active_turn
        return (active.task_id, active.accepting_input and not active.cancellation_token.is_cancelled) if active else (None, False)

    def configure_models(
        self,
        config: AppConfig,
        provider_factory: Callable[[ProviderConfig], ChatProvider] = create_provider,
    ) -> None:
        self._ensure_model_idle()
        self._models.configure_models(config, provider_factory)

    @property
    def model_config(self) -> AppConfig | None:
        return self._models.model_config

    @property
    def model_ref(self) -> str | None:
        return self._models.model_ref

    @property
    def model_notice(self) -> str:
        return self._models.model_notice

    def _ensure_model_idle(self) -> None:
        if not self._execution_runtime.accepting(self._session.session_id):
            raise ConfigError("会话正在停止或执行运行时已关闭，暂时不能开始新的操作。")
        if self.has_active_turn or self._manual_compaction:
            raise ConfigError("模型正在响应或压缩上下文，请等待完成后再切换模型。")

    def switch_model(self, model_ref: str) -> None:
        self._ensure_model_idle()
        self._models.switch_model(model_ref)

    def reload_models(self, config: AppConfig) -> bool:
        self._ensure_model_idle()
        return self._models.reload_models(config)

    def resume_session(self, session_id: str) -> int:
        self._ensure_model_idle()
        return self._models.resume_session(session_id)

    def new_session(self) -> None:
        self._ensure_model_idle()
        self._models.new_session()

    def set_phase(self, phase: WorkPhase) -> TurnEvent:
        self._ensure_model_idle()
        self._session.set_work_phase(phase)
        label = {"discuss": "讨论", "plan": "计划", "execute": "执行"}[phase]
        return TurnEvent(kind="phase_changed", work_phase=phase, progress_message=f"已切换到{label}阶段，权限策略不变")

    def set_permission_policy(self, policy: PermissionPolicy) -> TurnEvent:
        self._ensure_model_idle()
        self._session.set_permission_policy(policy)
        return TurnEvent(kind="policy_changed", permission_policy=policy, progress_message="权限策略已更新，工作阶段不变")

    @property
    def pending_inputs(self) -> list[PendingInput]:
        return self._inputs.items

    @property
    def queue_paused(self) -> bool:
        return self._inputs.paused

    def _pending_changed(self, items: list[PendingInput], item_id: str | None = None) -> None:
        self._session.update_pending_inputs(items)
        if self._active_turn is not None:
            self._active_turn.queue.put_nowait(TurnEvent(kind="pending_input_changed", pending_input_id=item_id))

    def enqueue_input(self, text: str, delivery: str = "follow_up"):
        return self._inputs.enqueue(text, delivery)

    def update_pending_input(self, item_id: str, text: str):
        return self._inputs.update(item_id, text)

    def remove_pending_input(self, item_id: str):
        return self._inputs.remove(item_id)

    def convert_pending_input(self, item_id: str, delivery: str):
        return self._inputs.convert(item_id, delivery)

    def pause_queue(self):
        return self._inputs.pause()

    def resume_queue(self):
        return self._inputs.resume()

    def _has_steering(self):
        return self._inputs.has_steering()

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
        selected = self._inputs.take_steering()
        message = self._session.complete_message(message_id, usage, record_transcript=False)
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
        item = self._inputs.take_next()
        if item is None:
            return
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
        if self._session.session_id is not None:
            self._execution_runtime.processes.seal_turn(self._session.session_id, self._active_turn.task_id)
        self._pause_queue_best_effort()
        if not self._active_turn.cancellation_token.is_cancelled:
            self._execution_runtime.invalidate(self._session.session_id)
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

    @property
    def is_compacting(self) -> bool:
        return self._manual_compaction

    @property
    def is_stopping(self) -> bool:
        active = self._active_turn
        return (not self._execution_runtime.accepting(self._session.session_id)
                or (active is not None and active.cancellation_token.is_cancelled)
                or (self._compaction_task is not None and self._compaction_task.cancelling() > 0))

    async def stop_and_wait(self) -> None:
        """界面关闭前等待当前任务收尾，不能把资源回收留给事件循环析构。"""
        active = self._active_turn
        if active is not None:
            self.cancel_active_turn()
            await asyncio.gather(active.task, return_exceptions=True)
            if self._active_turn is active:
                self._active_turn = None

    def _process_session_id(self, session_id=None) -> str:
        selected = session_id or self._session.session_id
        if selected is None:
            raise ValueError('发送第一条消息后才会创建 Session。')
        return selected

    def list_processes(self, session_id=None) -> list[dict]:
        if session_id is None and self._session.session_id is None:
            return []
        return [item.to_dict() for item in self._execution_runtime.processes.list(self._process_session_id(session_id))]

    def list_execution_tasks(self, session_id=None) -> list[dict]:
        return self._execution_runtime.list_invocations(session_id or self._session.session_id)

    def execution_summary(self, session_id=None) -> dict[str, int]:
        owner = session_id or self._session.session_id
        binding = self._execution_runtime.sessions.get(owner) if owner else None
        execution = binding.state.execution if binding else self._session.state.execution if owner == self._session.session_id else {}
        active = [item for item in execution.get('processes', {}).values()
                  if item['status'] in {'starting', 'running', 'stopping'}]
        waiting = sum(item['state'] in {'queued', 'awaiting_permission', 'waiting_resources'}
                      for item in execution.get('invocations', {}).values())
        return {'running': len(active), 'background': sum(item['lifetime'] == 'session' for item in active),
                'notifications': len(execution.get('inbox', [])), 'waiting': waiting}

    def read_process_output(self, process_id, cursor=0, max_chars=16000, session_id=None) -> dict:
        owner = self._process_session_id(session_id)
        supervisor = self._execution_runtime.processes
        return dict(supervisor.read(process_id, owner, cursor=cursor, max_chars=max_chars).to_dict(),
                    process=supervisor.get(process_id, owner).to_dict())

    async def stop_process(self, process_id, session_id=None):
        return await self._execution_runtime.processes.stop(process_id, self._process_session_id(session_id))

    async def background_process(self, process_id, session_id=None):
        return await self._execution_runtime.processes.background(process_id, self._process_session_id(session_id))

    async def write_process_input(self, process_id, text, session_id=None) -> None:
        # 只有用户实际操作任务界面才进入这里；模型写入仍须执行器审批。
        await self._execution_runtime.run_control(self._execution_runtime.processes.write(
            process_id, self._process_session_id(session_id), text))

    async def stop_session(self, session_id=None) -> None:
        owner = self._process_session_id(session_id)
        self._execution_runtime.begin_session_stop(owner)
        self._execution_runtime.processes.seal_session(owner)
        try:
            try:
                if owner == self._session.session_id:
                    await self.stop_and_wait()
            finally:
                await self._execution_runtime.processes.stop_session(owner)
        finally:
            self._execution_runtime.finish_session_stop(owner)

    async def shutdown(self) -> None:
        try:
            task = self._compaction_task
            if task is not None and task is not asyncio.current_task():
                if not task.done() and not task.cancelling():
                    task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            await self.stop_and_wait()
        finally:
            await self._execution_runtime.close()

    @property
    def application_process_count(self) -> int:
        """退出影响本应用托管的所有会话，不只统计当前画面。"""
        return self._execution_runtime.processes.active_count

    async def compact_context(
        self, *, activity_id: str | None = None,
        on_activity: Callable[[TurnEvent], Awaitable[None]] | None = None,
    ) -> ContextCompactionResult:
        return await self._compact_with_activity(
            "manual", activity_id=activity_id, on_activity=on_activity,
        )

    async def _compact_with_activity(
        self,
        trigger: CompactionTrigger,
        *,
        activity_id: str | None = None,
        message_id: str | None = None,
        turn_id: str | None = None,
        visible_tools=None,
        deferred_tool_groups=None,
        cancellation_token: CancellationToken | None = None,
        queue: asyncio.Queue[TurnEvent | object] | None = None,
        on_activity: Callable[[TurnEvent], Awaitable[None]] | None = None,
        continued_on_failure: bool = False,
    ) -> ContextCompactionResult:
        """三个入口共享一次活动，摘要内部重试不会创建第二条界面记录。"""
        activity = (self._session.get_compaction(activity_id) if activity_id is not None
                    else self._session.begin_compaction(trigger, message_id=message_id, turn_id=turn_id))
        manual_started = False

        async def publish(snapshot: CompactionActivity) -> None:
            message = self._session.get_message(snapshot.message_id) if snapshot.message_id else None
            event = TurnEvent(kind="compaction_updated", compaction=deepcopy(snapshot), message=message)
            if queue is not None:
                await self._emit(queue, event)
            if on_activity is not None:
                await on_activity(event)

        async def finish(status, **fields) -> None:
            try:
                snapshot = self._session.finish_compaction(activity.id, status=status, **fields)
            except Exception:
                # 终止事实可能已更新内存，但磁盘写入失败。界面仍结束等待，
                # 保存错误继续向上传递，不能把失败伪装成压缩完成。
                await publish(self._session.get_compaction(activity.id))
                raise
            await publish(snapshot)

        try:
            if trigger == "manual":
                if not self._execution_runtime.accepting(self._session.session_id):
                    raise ContextCompactionError("会话正在停止或执行运行时已关闭，暂时不能压缩上下文。")
                if self.has_active_turn or self._manual_compaction:
                    raise ContextCompactionError("模型正在响应，暂时不能压缩上下文。")
                self._manual_compaction = True
                self._compaction_task = asyncio.current_task()
                manual_started = True
            await publish(activity)
            if trigger == "manual":
                visible_tools = self._tool_registry.list_definitions(
                    discovered_names=set(), work_phase=self._session.work_phase,
                )
                deferred_tool_groups = self._tool_registry.list_deferred_index( work_phase=self._session.work_phase,
                )
            result = await self._session.compact_context(
                provider=self._models.provider,
                visible_tools=visible_tools,
                deferred_tool_groups=deferred_tool_groups,

                cancellation_token=cancellation_token,
                turn_id=turn_id,
                activity_id=activity.id,
            )
        except asyncio.CancelledError:
            await finish("cancelled")
            raise
        except Exception as exc:
            await finish(
                "failed", error_text=str(exc),
                continued=continued_on_failure and not isinstance(exc, SessionRepositoryError),
            )
            raise
        else:
            # Session 已把验证通过的上下文和完成活动一起持久化；这里只发布事实。
            await publish(self._session.get_compaction(activity.id))
            return result
        finally:
            if manual_started:
                self._manual_compaction = False
                self._compaction_task = None

    async def run_user_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        self._ensure_model_idle()
        queue: asyncio.Queue[TurnEvent | object] = asyncio.Queue()
        cancellation_token = CancellationToken()
        bound = asyncio.Event()

        async def run_bound_turn() -> None:
            # TUI 的 eager 调度可在 create_task 返回前执行；先等完整上下文发布。
            await bound.wait()
            await self._run_turn(text, active_turn)

        task = asyncio.create_task(run_bound_turn())
        # 启动前被取消的 Task 不会进入执行体的 finally，也必须结束事件消费者。
        task.add_done_callback(lambda _: queue.put_nowait(_QUEUE_END))
        try:
            active_turn = _ActiveTurn(task=task, queue=queue, cancellation_token=cancellation_token)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        self._active_turn = active_turn
        bound.set()

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
                if self._session.session_id is not None:
                    self._execution_runtime.processes.seal_turn(self._session.session_id, active_turn.task_id)
                    self._execution_runtime.invalidate(self._session.session_id)
                # 重复停止不能打断工具正在进行的子进程及管道清理。
                if not active_turn.task.cancelling():
                    active_turn.task.cancel()
                for future in active_turn.pending_permissions.values():
                    if not future.done():
                        future.cancel()
                self._pause_queue_best_effort()
            try:
                await asyncio.gather(active_turn.task, return_exceptions=True)
            finally:
                if self._active_turn is active_turn:
                    self._active_turn = None

    async def _run_turn(
        self,
        text: str,
        active_turn: _ActiveTurn,
    ) -> None:
        queue = active_turn.queue
        cancellation_token = active_turn.cancellation_token
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
        turn_id = active_turn.task_id
        owner_session_id = None
        generation = 0

        try:
            user_message = self._session.create_user_message(text)
            owner_session_id = self._session.session_id
            generation = self._execution_runtime.generation(owner_session_id)
            self._session.record_event('turn.started', turn_id=turn_id)
            await self._emit(queue, TurnEvent(kind="user_message_created", message=user_message))

            assistant_message = self._session.create_assistant_message()
            active_turn.assistant_message_id = assistant_message.id
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
                    work_phase=phase,
                )
                await self._session.offload_large_tool_results()
                deferred_tool_groups = self._tool_registry.list_deferred_index(
                    work_phase=phase,
                )
                request = self._session.build_request(
                    visible_tools,
                    allow_tool_calls=True,
                    work_phase=phase,
                    permission_policy=policy,
                    deferred_tool_groups=deferred_tool_groups,
                )
                request.cancellation_token = cancellation_token
                context_state = self._session.context_state
                estimated_tokens = self._session.estimate_request_tokens(request)
                budget = context_budget(self._session.context_window, request.max_output_tokens)
                if (
                    not context_state.automatic_compaction_disabled
                    and estimated_tokens >= budget.automatic_threshold
                ):
                    try:
                        compaction_result = await self._compact_with_activity(
                            "automatic", message_id=assistant_message.id, queue=queue,
                            visible_tools=visible_tools,
                            deferred_tool_groups=deferred_tool_groups,
                            cancellation_token=cancellation_token,
                            turn_id=turn_id,
                            continued_on_failure=estimated_tokens <= budget.input_limit,
                        )
                    except SessionRepositoryError:
                        raise
                    except Exception as exc:
                        # 完整候选验证失败时 Session 会恢复上下文副本；必须
                        # 更新当前状态，不能把失败计数写到已被替换的旧对象。
                        context_state = self._session.context_state
                        context_state.automatic_failure_count += 1
                        if context_state.automatic_failure_count >= AUTOMATIC_FAILURE_LIMIT:
                            context_state.automatic_compaction_disabled = True
                        logger.exception(
                            "event=automatic_context_compaction_failed context_id=%s failure_count=%s",
                            context_state.context_id,
                            context_state.automatic_failure_count,
                        )
                        if estimated_tokens > budget.input_limit:
                            raise ContextCompactionError(f"自动压缩失败：{exc}") from exc
                    else:
                        context_state = self._session.context_state
                        context_state.automatic_failure_count = 0
                        context_state.automatic_compaction_disabled = False
                        logger.info(
                            "event=automatic_context_compaction_succeeded context_id=%s before_tokens=%s after_tokens=%s",
                            context_state.context_id,
                            compaction_result.before_tokens,
                            compaction_result.after_tokens,
                        )
                        request = self._session.build_request(
                            visible_tools,
                            allow_tool_calls=True,
                            work_phase=phase,
                            permission_policy=policy,
                            deferred_tool_groups=deferred_tool_groups,
                        )
                        request.cancellation_token = cancellation_token

                budget = context_budget(self._session.context_window, request.max_output_tokens)
                if self._session.estimate_request_tokens(request) > budget.input_limit:
                    raise ContextCompactionError("当前请求仍超出可用输入预算，请缩减输入或压缩上下文。")

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
                    try:
                        response = await collect_response(
                            session=self._session, provider=self._models.provider,
                            turn_id=turn_id,
                            request=request,
                            assistant_message_id=assistant_message.id,
                            emit=partial(self._emit, queue),
                        )
                        loop_usage = response.usage
                        self._session.update_context_usage(request, loop_usage)
                        break
                    except ProviderPromptTooLongError as prompt_error:
                        if emergency_attempted:
                            raise
                        emergency_attempted = True
                        await self._session.offload_large_tool_results()
                        try:
                            result = await self._compact_with_activity(
                                "emergency", message_id=assistant_message.id, queue=queue,
                                visible_tools=visible_tools,
                                deferred_tool_groups=deferred_tool_groups,
                                cancellation_token=cancellation_token,
                                turn_id=turn_id,
                            )
                        except Exception:
                            raise prompt_error
                        if result.after_tokens > context_budget(self._session.context_window).input_limit:
                            raise
                        logger.info(
                            "event=emergency_context_compaction_succeeded context_id=%s before_tokens=%s after_tokens=%s dropped_groups=%s",
                            self._session.context_state.context_id,
                            result.before_tokens,
                            result.after_tokens,
                            result.dropped_groups,
                        )
                        request = self._session.build_request(
                            visible_tools,
                            allow_tool_calls=True,
                            work_phase=phase,
                            permission_policy=policy,
                            deferred_tool_groups=deferred_tool_groups,
                        )
                        request.cancellation_token = cancellation_token

                tool_calls, precomputed_results = response.tool_calls, response.precomputed_results
                # 每次请求只提交一次完整助手响应；工具反馈分支同样需要原思考数据。
                response_blocks = response.assistant_blocks
                if response_blocks:
                    self._session.append_assistant_response(response_blocks)

                total_usage = self._current_message_usage(assistant_message.id)
                await self._emit(
                    queue,
                    TurnEvent(
                        kind="usage_updated",
                        message=self._session.get_message(assistant_message.id),
                        usage=self._current_message_usage(assistant_message.id),
                    ),
                )

                if tool_calls:
                    if response.text:
                        self._session.clear_message_content(assistant_message.id)

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

                    results = await execute_batch(
                        session=self._session, executor=self._tool_executor,
                        tool_calls=tool_calls, precomputed_results=precomputed_results,
                        pending=pending_tool_calls, message_id=assistant_message.id,
                        turn_id=turn_id, generation=generation, phase=phase, policy=policy,
                        cancellation_token=cancellation_token, permission_resolver=self._request_permission,
                        available_tool_names={tool.name for tool in visible_tools},
                        should_interrupt=self._has_steering,
                        is_current=lambda: self._active_turn is active_turn,
                        emit=partial(self._emit, queue),
                    )
                    calls_by_id = {call.call_id: call for call in tool_calls}
                    for result in results:
                        discovered = result.metadata.get("discovered_tool_names")
                        if isinstance(discovered, list):
                            discovered_tool_names.update(
                                name for name in discovered if isinstance(name, str)
                            )
                        self._session.record_read_file_result(result)
                        if phase == "plan" and (not result.is_error) and result.tool_name == "write_plan_file":
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

                    unknown_tool_streak = next_unknown_tool_streak(unknown_tool_streak, results)
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
                active_turn.accepting_input = False
                message = self._session.complete_message(assistant_message.id, total_usage, record_transcript=False)
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
                    task_id=turn_id))
                return
        except asyncio.CancelledError:
            cancellation_token.cancel()
            message = self._finish_failed_message(assistant_message, pending_tool_calls, '本轮已取消。', cancelled=True)
            if message is not None:
                await self._emit(queue, TurnEvent(kind='turn_cancelled', message=message, progress_message='本轮已取消'))
        except Exception as exc:
            logger.exception('event=turn_failed exception_type=%s', type(exc).__name__)
            error_text = exc.user_message if isinstance(exc, LanCherError) else f'处理失败：{exc}'
            message = self._finish_failed_message(assistant_message, pending_tool_calls, error_text)
            await self._emit(queue, TurnEvent(kind='turn_failed', message=message, error_text=error_text))
        finally:
            try:
                if owner_session_id is not None:
                    # 正常结束与取消都结束 turn 作用域；session 进程继续托管。
                    await self._execution_runtime.processes.stop_turn(owner_session_id, turn_id)
                active_turn.accepting_input = False
                active_turn.pending_permissions.clear()
                if not completed:
                    self._pause_queue_best_effort()
                if self._session.session_id is not None:
                    kind = 'turn.completed' if completed else 'turn.interrupted' if cancellation_token.is_cancelled else 'turn.failed'
                    self._session.record_event(kind, turn_id=turn_id)
                self._session.flush()
            except Exception as exc:
                logger.exception('event=session_persistence_failed')
                await self._emit(queue, TurnEvent(kind='turn_failed', error_text=f'会话保存失败：{exc}'))

    def _pause_queue_best_effort(self) -> None:
        try:
            self.pause_queue()
        except Exception:
            logger.exception('event=pending_queue_persistence_failed')

    def _finish_failed_message(self, assistant, pending, text, *, cancelled=False):
        if assistant is None:
            return None
        try:
            close_pending_tool_calls(self._session, self._tool_registry, assistant.id, pending, text,
                                     invocation_records=self.list_execution_tasks())
        except Exception:
            logger.exception('event=interrupted_tools_persistence_failed')
        try:
            return self._session.cancel_message(assistant.id, text) if cancelled else self._session.fail_message(assistant.id, text)
        except Exception:
            logger.exception('event=terminal_message_persistence_failed')
            return self._session.get_message(assistant.id)

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

    def _current_message_usage(self, message_id: str) -> MessageUsage:
        usage = self._session.get_message(message_id).usage
        return deepcopy(usage)

    @staticmethod
    def _raise_if_cancelled(cancellation_token: CancellationToken) -> None:
        if cancellation_token.is_cancelled:
            raise asyncio.CancelledError
