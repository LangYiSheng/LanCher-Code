"""进程内 Agent 接口：统一运行、只读查询与资源所有权。"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from types import TracebackType
from typing import Literal
from uuid import uuid4

from lancher_code.agent.capabilities import AgentCapabilities
from lancher_code.agent.events import TurnEvent
from lancher_code.agent.results import AgentResult
from lancher_code.agent.runner import TurnRunner
from lancher_code.config.models import AgentConfig, AppConfig, RuntimeConfig
from lancher_code.context.models import CompactionActivity, ContextCompactionResult
from lancher_code.contracts.control import ExecutionAccess, WorkPhase
from lancher_code.contracts.json import json_value
from lancher_code.contracts.messages import ConversationMessage
from lancher_code.errors import ConfigError, LanCherError
from lancher_code.execution.contracts import ProcessInfo
from lancher_code.logging_system import get_logger
from lancher_code.mcp import MCPClientManager
from lancher_code.permissions.models import PermissionRequest, PermissionResolution
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.providers.models import ProviderConfig
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.models import PendingInput, PlanSnapshot, SessionMessage, SessionState
from lancher_code.sessions.paths import SessionPaths
from lancher_code.sessions.storage import SessionListing
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.usage.ledger import RunUsageSummary, RunUsageTracker

logger = get_logger("agent.service")


class AgentShutdownError(LanCherError):
    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("；".join(errors))


class AgentService:
    """同一个实例拥有一个活动会话，可由 TUI、脚本或服务适配器消费。"""

    def __init__(
        self, runner: TurnRunner, session: SessionController, *,
        usage_tracker: RunUsageTracker | None = None,
    ) -> None:
        self._runner, self._session = runner, session
        self._usage_tracker = usage_tracker
        self.run_id = getattr(usage_tracker, "run_id", None) or uuid4().hex
        self._sequence = 0
        self._session_epoch = 0
        self._observers: list[Callable[[TurnEvent], None]] = []
        self._stream_task: asyncio.Task | None = None
        self._runner_stream: AsyncIterator | None = None
        self._stream_stopped = False
        self._activity_task: asyncio.Task | None = None
        self._activity_callback: ContextVar[bool] = ContextVar("agent_activity_callback", default=False)
        self._close_task: asyncio.Task | None = None
        self._closed = False
        self._last_result: AgentResult | None = None
        self._runner.capabilities.add_mcp_progress_callback(self._mcp_progress)

    def _ensure_open(self) -> None:
        if self._closed or self._close_task is not None:
            raise ConfigError("Agent 已关闭或正在关闭。")

    def _ensure_idle(self) -> None:
        self._ensure_open()
        if self._stream_task is not None or self._activity_task is not None:
            raise ConfigError("Agent 正在运行，请等待完成或投递排队输入。")

    def _publish(self, event: TurnEvent) -> TurnEvent:
        self._sequence += 1
        snapshot = deepcopy(event)
        snapshot.session_id = self.session_id
        snapshot.run_id = self.run_id
        snapshot.sequence = self._sequence
        for observer in tuple(self._observers):
            try:
                observer(deepcopy(snapshot))
            except Exception:
                logger.exception("event=agent_observer_failed")
        return snapshot

    def _mcp_progress(self, progress) -> None:
        self._publish(TurnEvent(kind="mcp_progress_updated", mcp_progress=json_value(progress)))

    def subscribe(self, observer: Callable[[TurnEvent], None]) -> Callable[[], None]:
        """观察包括空闲命令在内的事件；回调应快速返回。"""
        self._ensure_open()
        self._observers.append(observer)
        def unsubscribe() -> None:
            if observer in self._observers:
                self._observers.remove(observer)
        return unsubscribe

    def start_capabilities(self) -> None:
        self._ensure_open()
        self._runner.start_capabilities()

    async def ready(self) -> None:
        self._ensure_open()
        await self.capabilities.ready()

    async def __aenter__(self) -> AgentService:
        try:
            await self.ready()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _close(self, caller: asyncio.Task | None) -> None:
        errors: list[str] = []
        try:
            await self._stop_and_wait(caller)
            if self._runner_stream is not None and not self._runner_stream.ag_running:
                await self._runner_stream.aclose()
        except Exception as exc:
            logger.exception("event=agent_stop_failed")
            errors.append(f"任务收尾失败：{exc}")
        finally:
            try:
                await self._runner.shutdown()
            except Exception as exc:
                logger.exception("event=agent_runner_shutdown_failed")
                errors.append(f"资源关闭失败：{exc}")
            finally:
                try:
                    self._session.close()
                except Exception as exc:
                    logger.exception("event=agent_session_close_failed")
                    errors.append(f"会话保存失败：{exc}")
                self._runner.capabilities.remove_mcp_progress_callback(self._mcp_progress)
                self._observers.clear()
                self._closed = True
                self._stream_task = None
        if errors:
            raise AgentShutdownError(tuple(errors))

    async def close(self) -> None:
        """重复关闭复用同一收尾任务，取消调用者也会等待资源关闭。"""
        if self._activity_task is not None and (asyncio.current_task() is self._activity_task or self._activity_callback.get()):
            raise ConfigError("压缩回调中不能关闭 Agent，请在压缩结束后关闭。")
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(asyncio.current_task()), name="agent-close")
        interrupted = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                interrupted = True
        self._close_task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def shutdown(self) -> None:
        await self.close()

    async def stop_and_wait(self) -> None:
        await self._stop_and_wait(asyncio.current_task(), wait_consumer=True)

    async def _stop_and_wait(self, caller, *, wait_consumer=False) -> None:
        self._stream_stopped = True
        stream_task = self._stream_task
        task = self._activity_task
        if task is not None and task is not caller:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._runner.stop_and_wait()
        if wait_consumer and stream_task is not None and stream_task is not caller:
            if not stream_task.done() and not stream_task.cancelling():
                stream_task.cancel()
            # 界面卸载可能已取消消费者；重复取消不能穿透其工具收尾。
            waiter = asyncio.gather(stream_task, return_exceptions=True)
            interrupted = False
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    interrupted = True
            waiter.result()
            if interrupted:
                raise asyncio.CancelledError

    def _record_result(self, event: TurnEvent) -> None:
        status = {"turn_completed": "completed", "turn_failed": "failed", "turn_cancelled": "cancelled"}.get(event.kind)
        if status is None:
            return
        message = event.message
        self._last_result = AgentResult(
            status=status, session_id=self.session_id,
            text=message.content if message is not None else "",
            usage=deepcopy(message.usage if message is not None else event.usage), error_text=event.error_text,
            run_usage=json_value(self.run_usage()),
        )

    async def _run_stream(self, text: str | None) -> AsyncIterator[TurnEvent]:
        self._ensure_open()
        if self._stream_task is not None or self._activity_task is not None:
            raise ConfigError("Agent 正在运行，请等待完成或投递排队输入。")
        self._stream_task = asyncio.current_task()
        self._stream_stopped = False
        self._last_result = None
        completed = False
        try:
            self.start_capabilities()
            stream = self._runner.run_next_queued_turn() if text is None else self._runner.run_user_turn(text)
            while True:
                completed = False
                self._runner_stream = stream
                async with aclosing(stream):
                    async for event in stream:
                        if self._closed:
                            return
                        snapshot = self._publish(event)
                        self._record_result(snapshot)
                        if event.kind == "turn_completed":
                            completed = True
                        elif event.kind in {"turn_cancelled", "turn_failed"}:
                            completed = False
                        yield snapshot
                if self._close_task is not None or self._stream_stopped:
                    break
                self._session.flush()
                items = self.pending_inputs
                if (not completed or self.queue_paused or not items
                        or items[0].state != "pending" or items[0].delivery != "follow_up"):
                    break
                stream = self._runner.run_next_queued_turn()
        finally:
            try:
                if not self._closed:
                    self._session.flush()
                if self._last_result is None:
                    self._last_result = AgentResult(status="cancelled", session_id=self.session_id,
                        usage=deepcopy(self.total_usage().usage), run_usage=json_value(self.run_usage()))
            finally:
                self._stream_task = None
                self._runner_stream = None

    async def run_user_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("输入不能为空。")
        async with aclosing(self._run_stream(text)) as events:
            async for event in events:
                yield event

    async def run_next_queued_turn(self) -> AsyncIterator[TurnEvent]:
        async with aclosing(self._run_stream(None)) as events:
            async for event in events:
                yield event

    async def run(
        self, text: str, *,
        permission_handler: Callable[
            [PermissionRequest], PermissionResolution | Awaitable[PermissionResolution]
        ] | None = None,
    ) -> AgentResult:
        """收集一次运行；无人处理的审批明确拒绝，不阻塞等待界面。"""
        async with aclosing(self.run_user_turn(text)) as events:
            async for event in events:
                request = event.permission_request
                if event.kind == "permission_request_created" and request is not None:
                    resolution = (PermissionResolution(request.request_id, "deny") if permission_handler is None
                                  else permission_handler(deepcopy(request)))
                    if inspect.isawaitable(resolution):
                        resolution = await resolution
                    if not isinstance(resolution, PermissionResolution) or resolution.request_id != request.request_id:
                        raise ValueError("审批处理器必须返回对应请求的 PermissionResolution。")
                    self.resolve_permission_request(resolution)
        if self._last_result is None:
            raise RuntimeError("运行结束但没有终止结果。")
        return deepcopy(self._last_result)

    async def compact_context(
        self, *, activity_id: str | None = None,
        on_activity: Callable[[TurnEvent], Awaitable[None]] | None = None,
    ) -> ContextCompactionResult:
        self._ensure_open()
        if self._stream_task is not None or self._activity_task is not None:
            raise ConfigError("Agent 正在运行，暂时不能压缩上下文。")
        self._activity_task = asyncio.current_task()
        async def publish(event):
            snapshot = self._publish(event)
            if on_activity is not None:
                token = self._activity_callback.set(True)
                try:
                    await on_activity(snapshot)
                finally:
                    self._activity_callback.reset(token)
        try:
            self.start_capabilities()
            return await self._runner.compact_context(activity_id=activity_id, on_activity=publish)
        finally:
            self._activity_task = None

    @property
    def last_result(self) -> AgentResult | None:
        return deepcopy(self._last_result)

    @property
    def session_epoch(self) -> int:
        return self._session_epoch

    @property
    def state(self) -> SessionState:
        return deepcopy(self._session.state)

    @property
    def messages(self) -> list[SessionMessage]:
        return deepcopy(self._session.state.messages)

    @property
    def compaction_activities(self) -> dict[str, CompactionActivity]:
        return deepcopy(self._session.state.compaction_activities)

    def snapshot(self) -> SessionState:
        return self.state

    @property
    def transcript(self) -> list[ConversationMessage]:
        return deepcopy(self._session.transcript)

    @property
    def project_root(self) -> Path:
        return self._session.project_root

    @property
    def paths(self) -> SessionPaths | None:
        return deepcopy(self._session.paths)

    @property
    def session_id(self) -> str | None:
        return self._session.session_id

    @property
    def session_title(self) -> str | None:
        return self._session.session_title

    @property
    def visited_session_ids(self) -> tuple[str, ...]:
        return self._session.visited_session_ids

    @property
    def permission_storage(self) -> PermissionStorage:
        return self._session.permission_storage

    @property
    def provider_config(self) -> ProviderConfig:
        return deepcopy(self._session.provider_config)

    @property
    def context_window(self) -> int:
        return self._session.context_window

    @property
    def work_phase(self) -> WorkPhase:
        return self._session.work_phase

    @property
    def execution_access(self) -> ExecutionAccess:
        return self._session.execution_access

    @property
    def plan_snapshot(self) -> PlanSnapshot | None:
        return self._session.plan_snapshot

    @property
    def plan_file_path(self) -> Path | None:
        return self._session.plan_file_path

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._runner.capabilities

    @property
    def model_config(self) -> AgentConfig | None:
        return deepcopy(self._runner.model_config)

    @property
    def model_ref(self) -> str | None:
        return self._runner.model_ref

    @property
    def model_notice(self) -> str:
        return self._runner.model_notice

    @property
    def pending_inputs(self) -> list[PendingInput]:
        return deepcopy(self._runner.pending_inputs)

    @property
    def queue_paused(self) -> bool:
        return self._runner.queue_paused

    @property
    def has_active_turn(self) -> bool:
        return self._runner.has_active_turn or self._stream_task is not None

    @property
    def is_compacting(self) -> bool:
        return self._activity_task is not None or self._runner.is_compacting

    @property
    def is_stopping(self) -> bool:
        return self._close_task is not None or self._runner.is_stopping

    @property
    def application_process_count(self) -> int:
        return self._runner.application_process_count

    def get_message(self, message_id: str) -> SessionMessage:
        return deepcopy(self._session.get_message(message_id))

    def get_compaction(self, activity_id: str) -> CompactionActivity:
        return deepcopy(self._session.get_compaction(activity_id))

    def total_usage(self) -> RunUsageSummary:
        return self._session.total_usage()

    def usage_summary(self, message_id: str | None = None) -> RunUsageSummary:
        return self._session.usage_summary(message_id)

    def run_usage(self) -> RunUsageSummary:
        return self._usage_tracker.snapshot() if self._usage_tracker is not None else self.total_usage()

    def list_sessions(self) -> SessionListing:
        return deepcopy(self._session.list_sessions())

    def session_exists(self, session_id: str) -> bool:
        return SessionPaths.for_session(self.project_root, session_id).events.is_file()

    def resume_session(self, session_id: str) -> int:
        self._ensure_idle()
        result = self._runner.resume_session(session_id)
        self._session_epoch += 1
        self._publish(TurnEvent(kind="session_changed"))
        return result

    def new_session(self) -> None:
        self._ensure_idle()
        self._runner.new_session()
        self._session_epoch += 1
        self._publish(TurnEvent(kind="session_changed"))

    def rename_session(self, session_id: str, title: str) -> None:
        self._ensure_idle()
        self._session.rename_session(session_id, title)

    def archive_session(self, session_id: str) -> None:
        self._ensure_idle()
        self._session.archive_session(session_id)

    def remove_session(self, session_id: str) -> None:
        self._ensure_idle()
        self._session.remove_session(session_id)

    def switch_model(self, model_ref: str) -> None:
        self._ensure_idle()
        self._runner.switch_model(model_ref)

    def reload_models(self, config: AgentConfig | AppConfig) -> bool:
        self._ensure_idle()
        return self._runner.reload_models(config)

    def validate_runtime_settings(self, runtime: RuntimeConfig) -> None:
        self._ensure_idle()
        self._runner.validate_runtime_settings(runtime)

    def apply_runtime_settings(self, runtime: RuntimeConfig) -> str:
        self._ensure_idle()
        return self._runner.apply_runtime_settings(runtime)

    def set_phase(self, phase: WorkPhase) -> TurnEvent:
        self._ensure_idle()
        return self._publish(self._runner.set_phase(phase))

    def set_execution_access(self, policy: ExecutionAccess) -> TurnEvent:
        self._ensure_idle()
        return self._publish(self._runner.set_execution_access(policy))

    def _queue_changed(self, operation, *args):
        self._ensure_open()
        result = operation(*args)
        if not self._runner.has_active_turn:
            self._publish(TurnEvent(kind="pending_input_changed"))
        return deepcopy(result)

    def enqueue_input(self, text: str, delivery: Literal["follow_up", "steer"] = "follow_up") -> PendingInput:
        return self._queue_changed(self._runner.enqueue_input, text, delivery)

    def update_pending_input(self, item_id: str, text: str) -> PendingInput:
        return self._queue_changed(self._runner.update_pending_input, item_id, text)

    def remove_pending_input(self, item_id: str) -> None:
        return self._queue_changed(self._runner.remove_pending_input, item_id)

    def convert_pending_input(self, item_id: str, delivery: Literal["follow_up", "steer"]) -> PendingInput:
        return self._queue_changed(self._runner.convert_pending_input, item_id, delivery)

    def pause_queue(self) -> None:
        return self._queue_changed(self._runner.pause_queue)

    def resume_queue(self) -> None:
        return self._queue_changed(self._runner.resume_queue)

    def prepare_plan_execution(self, session_id: str, digest: str) -> str:
        self._ensure_idle()
        return self._runner.prepare_plan_execution(session_id, digest)

    def resolve_permission_request(self, resolution: PermissionResolution) -> bool:
        self._ensure_open()
        return self._runner.resolve_permission_request(resolution)

    def cancel_active_turn(self) -> bool:
        self._stream_stopped = True
        if self._activity_task is not None:
            if not self._activity_task.cancelling():
                self._activity_task.cancel()
            return True
        return self._runner.cancel_active_turn()

    def configure_capabilities(self, **kwargs: MCPClientManager | ToolRegistry | None) -> None:
        self._ensure_open()
        self._runner.configure_capabilities(**kwargs)

    def list_processes(self, session_id: str | None = None) -> list[dict[str, object]]:
        return self._runner.list_processes(session_id)

    def list_execution_tasks(self, session_id: str | None = None) -> list[dict[str, object]]:
        return self._runner.list_execution_tasks(session_id)

    def execution_summary(self, session_id: str | None = None) -> dict[str, int]:
        return self._runner.execution_summary(session_id)

    def read_process_output(
        self, process_id: str, cursor: int = 0, max_chars: int = 16000,
        session_id: str | None = None,
    ) -> dict[str, object]:
        return self._runner.read_process_output(process_id, cursor, max_chars, session_id)

    async def stop_process(self, process_id: str, session_id: str | None = None) -> ProcessInfo:
        return await self._runner.stop_process(process_id, session_id)

    async def background_process(self, process_id: str, session_id: str | None = None) -> ProcessInfo:
        return await self._runner.background_process(process_id, session_id)

    async def write_process_input(self, process_id: str, text: str, session_id: str | None = None) -> None:
        self._ensure_open()
        await self._runner.write_process_input(process_id, text, session_id)

    async def stop_session(self, session_id: str | None = None) -> None:
        await self._runner.stop_session(session_id)
