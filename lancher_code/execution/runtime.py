"""应用执行上下文：保留后台 Session、持久化任务事件并隔离过期回调。"""
from __future__ import annotations

import copy
import asyncio
import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4
from lancher_code.logging_system import get_logger

from lancher_code.execution.contracts import ExecutionConfig, InvocationInfo, ResourceClaim
from lancher_code.execution.processes import ProcessSupervisor, TERMINAL_STATUSES

logger = get_logger('execution.runtime')

if TYPE_CHECKING:
    from lancher_code.models import SessionState, ConversationMessage, PermissionRule, ProviderConfig
    from lancher_code.sessions.service import SessionService


@dataclass(slots=True)
class SessionRuntime:
    """服务与状态的绑定始终指向原 Session，不通过当前界面寻找写入者。"""
    service: SessionService
    state: SessionState
    transcript: list[ConversationMessage]
    rules: list[PermissionRule]
    model_ref: str | None
    provider_config: ProviderConfig


class SessionRuntimeRegistry:
    def __init__(self) -> None:
        self._items: dict[str, SessionRuntime] = {}

    def get(self, session_id: str) -> SessionRuntime | None:
        return self._items.get(session_id)

    def items(self):
        return tuple(self._items.items())

    def register(self, binding: SessionRuntime) -> None:
        session_id = binding.state.session_id
        if session_id is None:
            return
        old = self._items.get(session_id)
        if old is not None and old.service is not binding.service:
            raise ValueError("同一个 Session 不能注册两个写入者。")
        self._items[session_id] = binding

    def release(self, session_id: str, *, checkpoint=True, force=False) -> None:
        binding = self._items.get(session_id)
        if binding is not None:
            try:
                if checkpoint:
                    binding.service.checkpoint()
            except Exception:
                if force:
                    self._items.pop(session_id, None)
                    binding.service.close()
                raise
            else:
                self._items.pop(session_id, None)
                binding.service.close()

    def close(self) -> None:
        first_error = None
        for session_id in list(self._items):
            try:
                self.release(session_id, force=True)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


class ExecutionRuntime:
    def __init__(self, project_root: Path, config: ExecutionConfig | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config or ExecutionConfig()
        self.limits = self.config.limits
        self.sessions = SessionRuntimeRegistry()
        self._generations: dict[str, int] = {}
        self._session_stops: dict[str, int] = {}
        self._invocations: dict[str, InvocationInfo] = {}
        self._closed = False
        self._view_session_id = None
        self._close_task = None
        self._control_tasks: set[asyncio.Task] = set()
        self.processes = ProcessSupervisor(self.project_root, limits=self.limits, event_sink=self.record_event,
                                           saved_processes=self.saved_processes)

    def register_session(self, binding: SessionRuntime, *, viewed=True) -> None:
        if self._closed:
            raise ValueError("执行运行时已关闭。")
        self.sessions.register(binding)
        if viewed:
            self._view_session_id = binding.state.session_id

    def detach_view(self, session_id: str) -> None:
        if self._view_session_id == session_id:
            self._view_session_id = None

    def _release_idle_sessions(self) -> None:
        if self._control_tasks:
            return
        for session_id, binding in self.sessions.items():
            if session_id != self._view_session_id and not self.processes.active_session(session_id):
                try:
                    self.sessions.release(session_id)
                except Exception:
                    # checkpoint 是缓存；失败时保留原 writer，以后仍可重试关闭。
                    logger.exception('event=inactive_session_release_failed')

    def generation(self, session_id: str | None) -> int:
        return self._generations.get(session_id or "", 0)

    def is_current(self, session_id: str | None, generation: int) -> bool:
        return self.accepting(session_id) and generation == self.generation(session_id)

    def accepting(self, session_id: str | None) -> bool:
        return not self._closed and not self._session_stops.get(session_id or "", 0)

    def begin_session_stop(self, session_id: str) -> None:
        # 当前轮次先结束、后台稍后收尾；整个范围都不能接收新副作用。
        self._session_stops[session_id] = self._session_stops.get(session_id, 0) + 1
        self.invalidate(session_id)

    def finish_session_stop(self, session_id: str) -> None:
        remaining = self._session_stops[session_id] - 1
        if remaining:
            self._session_stops[session_id] = remaining
        else:
            self._session_stops.pop(session_id)

    def invalidate(self, session_id: str | None) -> None:
        key = session_id or ""
        self._generations[key] = self._generations.get(key, 0) + 1

    def begin_invocation(self, call, context) -> InvocationInfo:
        now = datetime.now(timezone.utc).isoformat()
        info = InvocationInfo(uuid4().hex, call.call_id, context.session_id or "", context.turn_id,
                              context.generation, call.tool_name, started_at=now, updated_at=now)
        self._invocations[info.invocation_id] = info
        self.record_event(context.session_id, "invocation.queued", info.to_dict(), turn_id=info.turn_id)
        return info

    def update_invocation(self, info: InvocationInfo, state: str, **changes) -> None:
        info.state = state
        info.updated_at = datetime.now(timezone.utc).isoformat()
        for key, value in changes.items():
            if not hasattr(info, key):
                raise ValueError(f"未知调用字段：{key}")
            setattr(info, key, value)
        self.record_event(info.session_id or None, f"invocation.{state}", info.to_dict(), turn_id=info.turn_id)

    def record_event(self, session_id, event_type, data, *, turn_id=None) -> None:
        if session_id is None:
            return  # 单独调用执行器的测试或嵌入模式不自动创建空 Session。
        binding = self.sessions.get(session_id)
        if binding is None:
            if event_type.startswith("process."):
                raise ValueError("进程启动前必须注册所属 Session 的写入者。")
            return
        payload = copy.deepcopy(data)
        if event_type == "process.exited":
            payload["notification_id"] = uuid4().hex
        binding.service.record_execution(event_type, payload, turn_id=turn_id)
        # 先提交日志，再更新两个投影；落盘失败绝不会假装操作已发生。
        from lancher_code.sessions.codec import SessionCodec
        SessionCodec.apply_execution_event(binding.state.execution, event_type, payload)
        if event_type == 'process.exited' and not self._closed:
            try:
                # monitor 在提交事件后才标记 done；下一轮事件循环再判断活动资源。
                asyncio.get_running_loop().call_soon(self._release_idle_sessions)
            except RuntimeError:
                pass

    def list_invocations(self, session_id: str | None) -> list[dict]:
        binding = self.sessions.get(session_id) if session_id else None
        if binding is not None:
            return list(copy.deepcopy(binding.state.execution["invocations"]).values())
        return [item.to_dict() for item in self._invocations.values() if item.session_id == (session_id or "")]

    def saved_processes(self, session_id: str) -> list[dict]:
        """日志投影是历史状态事实；进程目录里的 meta 只是查询缓存。"""
        binding = self.sessions.get(session_id)
        if binding is None:
            return []
        return list(copy.deepcopy(binding.state.execution['processes']).values())

    def command_claims(self, command: str, cwd: Path) -> tuple[ResourceClaim, ...]:
        from lancher_code.execution.scheduler import path_claim, project_claim
        command = command.strip()
        for profile in self.config.command_profiles:
            if fnmatch.fnmatchcase(command, profile.command_match):
                claims = []
                for claim in profile.resources:
                    key = claim.key
                    if claim.kind == "path":
                        path = Path(key)
                        key = str((path if path.is_absolute() else self.project_root / path).resolve())
                    if claim.kind == 'path':
                        claims.append(path_claim(Path(key), write=claim.mode == 'exclusive', recursive=claim.recursive))
                    elif claim.kind == 'project':
                        path = Path(key)
                        project = project_claim(path if path.is_absolute() else self.project_root / path)
                        claims.append(ResourceClaim('project', project.key, claim.mode, True))
                    else:
                        claims.append(ResourceClaim(claim.kind, key, claim.mode, claim.recursive))
                return tuple(claims)
        return (project_claim(self.project_root),)

    def command_readiness(self, command: str):
        command = command.strip()
        for profile in self.config.command_profiles:
            if fnmatch.fnmatchcase(command, profile.command_match):
                return profile.readiness
        return None

    async def run_control(self, operation) -> object:
        """用户已提交的控制操作属于运行时；关闭详情页只结束观察者。"""
        if self._closed:
            operation.close()
            raise ValueError('执行运行时已关闭。')
        task = asyncio.create_task(operation)
        self._control_tasks.add(task)

        def finished(completed):
            self._control_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error('event=process_control_failed exception_type=%s', type(completed.exception()).__name__)
            if not self._closed:
                self._release_idle_sessions()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def recover_session(self, session_id: str) -> None:
        """只恢复记录；不认领旧 PID、不执行旧命令。"""
        binding = self.sessions.get(session_id)
        if binding is None:
            return
        for info in list(binding.state.execution["processes"].values()):
            if info["status"] not in TERMINAL_STATUSES:
                lost = dict(info, status="lost", readiness="unknown", exit_reason="application_restarted")
                self.record_event(session_id, "process.exited", lost, turn_id=info.get("origin_turn_id"))
        terminal = {"succeeded", "failed", "cancelled", "superseded", "interrupted"}
        for info in list(binding.state.execution["invocations"].values()):
            if info["state"] not in terminal:
                self.record_event(session_id, "invocation.interrupted", dict(info, state="interrupted"),
                                  turn_id=info.get("turn_id"))

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._finish_close())
        # 共享清理任务：两个关闭入口都等到回收完成，取消调用者也不留下系统资源。
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                continue
        return self._close_task.result()

    async def _finish_close(self) -> None:
        try:
            await self.processes.close()
        finally:
            try:
                if self._control_tasks:
                    await asyncio.gather(*self._control_tasks, return_exceptions=True)
            finally:
                self.sessions.close()
