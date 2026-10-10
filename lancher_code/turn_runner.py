from __future__ import annotations

import asyncio
from contextlib import aclosing
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4

from lancher_code.context_management import AUTOMATIC_FAILURE_LIMIT
from lancher_code.context_budget import context_budget
from lancher_code.execution.contracts import InvocationInfo
from lancher_code.errors import (
    ConfigError,
    ContextCompactionError,
    ProviderResponseError,
    LanCherError,
    ProviderPromptTooLongError,
)
from lancher_code.logging_system import get_logger, register_sensitive_values
from lancher_code.model_catalog import iter_model_refs, model_display_name, resolve_model
from lancher_code.models import (
    AppConfig,
    CancellationToken,
    ChatRequest,
    CompactionActivity,
    CompactionTrigger,
    ContextCompactionResult,
    ContentBlock,
    MessageUsage,
    PendingInput,
    PermissionPolicy,
    PermissionRequest,
    PermissionResolution,
    ProviderConfig,
    ToolCall,
    ToolExecutionResult,
    TurnEvent,
    WorkPhase,
)
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.factory import create_provider
from lancher_code.session import SessionController
from lancher_code.sessions.repository import SessionRepositoryError
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
        self.assistant_blocks: list[ContentBlock] | None = None
        self.stop_reason: str | None = None

    def append(self, delta: str) -> None:
        self._text_parts.append(delta)

    @property
    def text(self) -> str:
        return "".join(self._text_parts)

    def response_blocks(self, tool_calls: list[ToolCall]) -> list[ContentBlock]:
        # Provider快照保留单次响应的原顺序和签名；旧测试流仅提供正文/工具增量。
        if self.assistant_blocks is not None:
            return deepcopy(self.assistant_blocks)
        blocks = [ContentBlock.text_block(self.text)] if self.text else []
        blocks.extend(ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name,
                                                input=deepcopy(call.arguments)) for call in tool_calls)
        return blocks


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
        self._execution_runtime = tool_executor.execution_runtime
        self._session.bind_execution_runtime(self._execution_runtime)
        self._max_tool_loops = max_tool_loops
        self._unknown_tool_streak_limit = unknown_tool_streak_limit
        self._active_turn: _ActiveTurn | None = None
        self._manual_compaction = False
        self._compaction_task: asyncio.Task | None = None
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
        if not self._execution_runtime.accepting(self._session.session_id):
            raise ConfigError("会话正在停止或执行运行时已关闭，暂时不能开始新的操作。")
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

    def resume_session(self, session_id: str) -> int:
        self._ensure_model_idle()
        if self._model_config is None:
            self._model_notice = ''
            return self._session.resume_session(session_id)
        saved_ref = self._session.read_session_model_ref(session_id)
        config = self._require_model_config()
        target = saved_ref if saved_ref in iter_model_refs(config) else config.default_model
        resolved, provider = self._prepare_model(config, target)
        permission_count = self._session.resume_session(
            session_id, resolved_model=(resolved, target)
        )
        self._provider = provider
        if saved_ref is None:
            self._model_notice = f"会话未选择模型，已使用默认模型：{model_display_name(config, target)}。"
        elif saved_ref != target:
            self._model_notice = f"会话原模型已不存在，已使用默认模型：{model_display_name(config, target)}。"
        else:
            self._model_notice = ""
        return permission_count

    def new_session(self) -> None:
        self._ensure_model_idle()
        if self._model_config is not None:
            target = self._model_config.default_model
            resolved, provider = self._prepare_model(self._model_config, target)
            self._session.new_session()
            self._session.set_model(resolved, target, initial=True)
            self._provider = provider
        else:
            self._session.new_session()
        self._model_notice = ''

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
                    discovered_names=set(), mode=self._session.runtime_mode, work_phase=self._session.work_phase,
                )
                deferred_tool_groups = self._tool_registry.list_deferred_index(
                    mode=self._session.runtime_mode, work_phase=self._session.work_phase,
                )
            result = await self._session.compact_context(
                provider=self._provider,
                visible_tools=visible_tools,
                deferred_tool_groups=deferred_tool_groups,
                persist=trigger == "manual",
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
                            mode=self._session.runtime_mode,
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
                            mode=self._session.runtime_mode,
                            work_phase=phase,
                            permission_policy=policy,
                            deferred_tool_groups=deferred_tool_groups,
                        )
                        request.cancellation_token = cancellation_token

                tool_calls, precomputed_results = assembler.finalize_batch(stop_reason=collector.stop_reason)
                # 每次请求只提交一次完整助手响应；工具反馈分支同样需要原思考数据。
                response_blocks = collector.response_blocks(tool_calls)
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
                    if collector.text:
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

                    recorded_ids: set[str] = set()

                    async def report_invocation_state(call: ToolCall, info: InvocationInfo) -> None:
                        # 只把当前轮次的真实执行投影送到时间线；审批结束不等于工具已启动。
                        if (self._active_turn is not active_turn or info.turn_id != turn_id
                                or info.session_id != self._session.session_id
                                or call.call_id in recorded_ids):
                            return
                        labels = {
                            "queued": "等待执行", "awaiting_permission": "等待批准",
                            "waiting_resources": "等待资源 · 审批已通过", "running": "正在执行",
                        }
                        if info.state not in labels:
                            return
                        message = self._session.set_trace_tool_state(
                            assistant_message.id, call.call_id, info.state,
                            waiting=info.waiting, invocation_id=info.invocation_id)
                        await self._emit(queue, TurnEvent(kind="progress_updated", message=message,
                            tool_call=call, progress_message=f"{labels[info.state]} · {call.tool_name}"))

                    async def report_started(call: ToolCall) -> None:
                        message = self._session.set_trace_tool_state(assistant_message.id, call.call_id, "running")
                        self._session.record_event('tool.started', {'call_id': call.call_id, 'tool_name': call.tool_name},
                                                   turn_id=turn_id)
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
                        self._session.record_event('tool.finished', {'call_id': result.call_id, 'ok': result.ok},
                                                   turn_id=turn_id)
                        await self._emit(queue, TurnEvent(kind="tool_result_received", message=message,
                            usage=self._current_message_usage(assistant_message.id), tool_result=result))

                    results = precomputed_results or await self._tool_executor.execute_calls(
                        tool_calls,
                        mode=self._session.runtime_mode,
                        work_phase=phase,
                        permission_policy=policy,
                        plan_file_path=self._session.plan_file_path,
                        session_id=self._session.session_id,
                        session_workspace=self._session.paths.workspace if self._session.paths else None,
                        session_root=self._session.paths.root if self._session.paths else None,
                        cancellation_token=cancellation_token,
                        turn_id=turn_id,
                        generation=generation,
                        permission_resolver=self._request_permission,
                        available_tool_names={tool.name for tool in visible_tools},
                        should_interrupt=self._has_steering,
                        on_call_started=report_started,
                        on_invocation_state=report_invocation_state,
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
            self._close_pending_tool_calls(assistant.id, pending, text)
        except Exception:
            logger.exception('event=interrupted_tools_persistence_failed')
        try:
            return self._session.cancel_message(assistant.id, text) if cancelled else self._session.fail_message(assistant.id, text)
        except Exception:
            logger.exception('event=terminal_message_persistence_failed')
            return self._session.get_message(assistant.id)

    def _close_pending_tool_calls(self, message_id: str, pending: list[ToolCall], reason: str) -> None:
        if not pending:
            return
        # 仅补齐尚未记录结果的调用，不能把中断误报为工具完全没有执行。
        trace = self._session.get_message(message_id).trace.entries
        invocations = {item['invocation_id']: item for item in self.list_execution_tasks()}
        results = []
        for call in pending:
            entry = next((item for item in reversed(trace) if item.kind == 'tool_call' and item.call_id == call.call_id), None)
            started = bool(entry and entry.metadata.get('started'))
            try:
                definition = self._tool_registry.get(call.tool_name).definition
                external = bool(definition.permission and definition.permission.source == 'external' and definition.category != 'read')
            except Exception:
                external = False
            invocation = invocations.get(entry.metadata.get('invocation_id')) if entry else None
            if (external and invocation and invocation['state'] == 'cancelled'
                    and invocation.get('error_code') is None):
                # running 状态通知可先让出控制权。执行器只有真正进入远端
                # execute 后才把取消记为 interrupted / outcome_unknown。
                started = False
                entry.metadata['started'] = False
            unknown_remote = started and external
            message = (f'{reason}，远端操作结果未知；停止本地等待不能证明远端已撤销。请先检查远端状态，勿直接重复提交。'
                       if unknown_remote else
                       f'{reason}，未获得此工具调用的完整结果。操作可能已部分执行，请先检查当前状态，勿直接重复执行。'
                       if started else f'{reason}，此工具尚未启动，没有执行。')
            results.append(ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                content=message,
                is_error=True,
                summary='远端结果未知' if unknown_remote else '工具结果未完成' if started else '工具未启动',
                error_code='mcp_outcome_unknown' if unknown_remote else 'tool_result_interrupted',
                error_message=message,
                metadata={'started': started, 'outcome': 'unknown' if started else 'not_started'},
            ))
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
        self._session.bind_usage_request(request, turn_id=self._active_turn.task_id if self._active_turn else None,
                                         message_id=assistant_message_id)
        async with aclosing(self._session.stream_request(self._provider, request)) as stream:
            async for event in stream:
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
                    if event.response_complete is False:
                        raise ProviderResponseError("模型响应流提前结束，未收到完整响应；本次工具没有执行，请重试。")
                    collector.assistant_blocks = deepcopy(event.assistant_blocks)
                    collector.stop_reason = event.stop_reason
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
        return deepcopy(usage)

    @staticmethod
    def _raise_if_cancelled(cancellation_token: CancellationToken) -> None:
        if cancellation_token.is_cancelled:
            raise asyncio.CancelledError
