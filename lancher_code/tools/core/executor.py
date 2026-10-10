from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from lancher_code.errors import ToolNotFoundError
from lancher_code.execution.contracts import InvocationInfo, ResourceClaim, ResourceOwner
from lancher_code.execution.scheduler import ResourceScheduler, claims_conflict, get_project_scheduler, normalize_claim, path_claim, project_claim
from lancher_code.logging_system import get_logger
from lancher_code.contracts.control import CancellationToken, WorkPhase, PermissionPolicy
from lancher_code.permissions.models import PermissionRequest, PermissionResolution
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult, tool_available_in_phase
from lancher_code.tools.context import ToolContext
from lancher_code.permissions.models import PermissionCheck
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.filesystem.access import resolve_path_in_root
from lancher_code.tools.core.file_state_cache import FileStateCache
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tools.core.base import Tool
from lancher_code.tools.core.validation import validate_tool_arguments

if TYPE_CHECKING:
    from lancher_code.execution.runtime import ExecutionRuntime

logger = get_logger("tools.executor")
PermissionResolver = Callable[[PermissionRequest], Awaitable[PermissionResolution]]
ToolStartedCallback = Callable[[ToolCall], Awaitable[None]]
ToolResultCallback = Callable[[ToolExecutionResult], Awaitable[None]]
InvocationStateCallback = Callable[[ToolCall, InvocationInfo], Awaitable[None]]


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, *, cwd: Path, timeout_seconds: float = 10.0,
        permission_engine: PermissionEngine | None = None,
        execution_runtime: ExecutionRuntime | None = None,
    ) -> None:
        from lancher_code.execution.runtime import ExecutionRuntime

        self._registry = registry
        self._cwd = cwd.resolve()
        self._timeout_seconds = timeout_seconds
        self._file_state_caches: dict[str | None, FileStateCache] = {}
        self._permission_engine = permission_engine or PermissionEngine()
        self.execution_runtime = execution_runtime or ExecutionRuntime(self._cwd)

    async def execute_calls(
        self, calls: list[ToolCall], *,
        work_phase: WorkPhase = "execute", permission_policy: PermissionPolicy = "default",
        plan_file_path: Path | None = None, session_id: str | None = None,
        session_workspace: Path | None = None, session_root: Path | None = None,
        turn_id: str | None = None, generation: int | None = None,
        cancellation_token: CancellationToken | None = None,
        permission_resolver: PermissionResolver | None = None,
        available_tool_names: set[str] | None = None,
        expected_tool_bindings: dict[str, Tool] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
        on_call_started: ToolStartedCallback | None = None, on_result: ToolResultCallback | None = None,
        on_invocation_state: InvocationStateCallback | None = None,
    ) -> list[ToolExecutionResult]:
        context = ToolContext(
            cwd=self._cwd, timeout_seconds=self._timeout_seconds,
            work_phase=work_phase, permission_policy=permission_policy,
            project_root=self._cwd, plan_file_path=plan_file_path, session_id=session_id,
            session_workspace=session_workspace, session_root=session_root,
            cancellation_token=cancellation_token,
            file_state_cache=self._file_state_caches.setdefault(session_id, FileStateCache()),
            execution_runtime=self.execution_runtime, turn_id=turn_id,
            generation=self.execution_runtime.generation(session_id) if generation is None else generation,
        )
        scheduler = get_project_scheduler(self._cwd, max_concurrency=self.execution_runtime.limits.max_concurrency)
        # 冻结参数，防止审批期间调用方修改实际执行内容。
        frozen_calls = [replace(call, arguments=deepcopy(call.arguments)) for call in calls]
        expected_bindings = dict(expected_tool_bindings) if expected_tool_bindings is not None else None
        claims = [self._resource_claims(call, context) if self._argument_error(call) is None else () for call in frozen_calls]
        completed = [asyncio.Event() for _ in calls]
        invocations = [self.execution_runtime.begin_invocation(call, context) for call in frozen_calls]
        tasks = []
        for index, call in enumerate(frozen_calls):
            predecessors = [(completed[earlier], invocations[earlier]) for earlier in range(index)
                            if any(claims_conflict(one, other) for one in claims[earlier] for other in claims[index])]
            tasks.append(asyncio.create_task(self._execute_one(
                call, replace(context), invocations[index], scheduler, claims[index], predecessors, completed[index],
                permission_resolver, available_tool_names, should_interrupt, on_call_started, on_result, on_invocation_state,
                expected_bindings,
            )))
        try:
            # gather 保留输入顺序，on_result 按实际完成顺序立即推送。
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # 业务失败已转换为结构化结果；只有取消或日志/通知基础设施失败才收束整批。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _argument_error(self, call: ToolCall) -> ToolExecutionResult | None:
        try:
            tool = self._registry.get(call.tool_name)
        except ToolNotFoundError:
            return None
        issue = validate_tool_arguments(call.arguments, tool.definition.input_schema)
        if issue is None:
            return None
        return self._error(call, issue.error_code, issue.error_message,
                           {"argument_path": list(issue.path), "constraint": issue.constraint})

    def _resource_claims(self, call: ToolCall, context: ToolContext) -> tuple[ResourceClaim, ...]:
        root = context.project_root or context.cwd
        try:
            tool = self._registry.get(call.tool_name)
            declaration = getattr(tool, "resource_claims", None)
            if callable(declaration):
                claims = tuple(declaration(call.arguments, context))
                if not all(isinstance(claim, ResourceClaim) for claim in claims):
                    raise TypeError("工具资源声明必须返回 ResourceClaim。")
                return tuple(normalize_claim(claim) for claim in claims)
            # MCP 的 readOnlyHint 描述远端能力，不足以证明与本地或其他远端操作无冲突。
            if tool.definition.permission is not None and tool.definition.permission.source == "external":
                return (project_claim(root),)
            name = call.tool_name
            if name in {"read_file", "write_file", "edit_file"}:
                path = resolve_path_in_root(context.cwd, str(call.arguments.get("path", "")), root)
                return (path_claim(path, write=name != "read_file"),)
            if name == "write_plan_file" and context.plan_file_path is not None:
                return (path_claim(context.plan_file_path, write=True),)
            if name in {"glob", "grep"}:
                raw = call.arguments.get("path")
                path = resolve_path_in_root(context.cwd, raw, root) if isinstance(raw, str) and raw.strip() else root
                return (path_claim(path, recursive=True),)
            if name == "run_command":
                raw = call.arguments.get("cwd")
                cwd = resolve_path_in_root(context.cwd, raw, root) if isinstance(raw, str) and raw.strip() else context.cwd
                return tuple(normalize_claim(claim) for claim in self.execution_runtime.command_claims(str(call.arguments.get("command", "")), cwd))
            if name in {"process_write", "process_background"}:
                return (ResourceClaim("process", f"{call.arguments.get('process_id', '')}:stdin", "exclusive"),)
            if name in {"tool_search", "process_list", "process_read", "process_wait", "process_stop"}:
                return ()
        except (ToolNotFoundError, ValueError, TypeError):
            # 无效参数由工具给出具体错误；保守资源声明不会让坏参数获得更宽并行权限。
            pass
        return (project_claim(root),)

    async def _execute_one(
        self, call: ToolCall, context: ToolContext, invocation: InvocationInfo, scheduler: ResourceScheduler,
        claims: tuple[ResourceClaim, ...], predecessors: list[tuple[asyncio.Event, InvocationInfo]], completed: asyncio.Event,
        permission_resolver: PermissionResolver | None, available_tool_names: set[str] | None,
        should_interrupt: Callable[[], bool] | None, on_call_started: ToolStartedCallback | None,
        on_result: ToolResultCallback | None, on_invocation_state: InvocationStateCallback | None,
        expected_tool_bindings: dict[str, Tool] | None = None,
    ) -> ToolExecutionResult:
        context.invocation_id = invocation.invocation_id
        try:
            unresolved = [info for event, info in predecessors if not event.is_set()]
            if unresolved:
                await self._report_state(call, invocation, "queued", on_invocation_state,
                                         waiting=self._predecessor_snapshot(unresolved))
            elif on_invocation_state is not None:
                await on_invocation_state(call, deepcopy(invocation))
            predecessor_waits = {asyncio.create_task(event.wait()) for event, _ in predecessors if not event.is_set()}
            pending_waits = predecessor_waits.copy()
            try:
                while pending_waits:
                    # 前序可能乱序完成；只显示仍未结束者，执行仍须等待全部冲突前序。
                    await self._await_cancelable(asyncio.wait(pending_waits, return_when=asyncio.FIRST_COMPLETED), context)
                    pending_waits = {wait for wait in pending_waits if not wait.done()}
                    remaining = [info for event, info in predecessors if not event.is_set()]
                    if remaining and remaining != unresolved:
                        await self._report_state(call, invocation, "queued", on_invocation_state,
                                                 waiting=self._predecessor_snapshot(remaining))
                    unresolved = remaining
            finally:
                for wait in predecessor_waits:
                    if not wait.done():
                        wait.cancel()
                await asyncio.gather(*predecessor_waits, return_exceptions=True)
            result = await self._run_one(call, context, invocation, scheduler, claims, permission_resolver,
                                         available_tool_names, should_interrupt, on_call_started, on_invocation_state,
                                         expected_tool_bindings)
            state = ("interrupted" if result.error_code == "mcp_outcome_unknown" else
                     "superseded" if result.error_code == "steering_superseded" else
                     "failed" if result.is_error else "succeeded")
            await self._report_state(call, invocation, state, on_invocation_state, error_code=result.error_code,
                                     process_id=result.metadata.get("process_id"))
            if on_result is not None:
                await on_result(result)
            return result
        except asyncio.CancelledError:
            if invocation.state not in {"succeeded", "failed", "interrupted", "superseded"}:
                # running 通知本身可让出控制权；真正进入外部工具后的取消在 _run_one 标记未知结果。
                await self._report_state(call, invocation, "cancelled", on_invocation_state)
            raise
        finally:
            # 后台只保留显式进程资源；调用排序锁在句柄返回后结束。
            if context.resource_lease is not None:
                await context.resource_lease.finish_invocation()
            completed.set()

    @staticmethod
    def _predecessor_snapshot(predecessors: list[InvocationInfo]) -> dict:
        return {"reason": "predecessors", "blockers": [
            {"session_id": info.session_id or None, "invocation_id": info.invocation_id,
             "tool_name": info.tool_name, "process_id": info.process_id, "resources": []} for info in predecessors]}

    async def _report_state(self, call: ToolCall, invocation: InvocationInfo, state: str,
                            callback: InvocationStateCallback | None, **changes) -> None:
        changes.setdefault("waiting", {})
        self.execution_runtime.update_invocation(invocation, state, **changes)
        if callback is not None:
            # 记录是事实来源；界面拿到独立快照，不能反过来更改真实调用状态。
            await callback(call, deepcopy(invocation))

    async def _run_one(
        self, call: ToolCall, context: ToolContext, invocation: InvocationInfo,
        scheduler: ResourceScheduler, claims: tuple[ResourceClaim, ...],
        permission_resolver: PermissionResolver | None, available_tool_names: set[str] | None,
        should_interrupt: Callable[[], bool] | None, on_call_started: ToolStartedCallback | None,
        on_invocation_state: InvocationStateCallback | None,
        expected_tool_bindings: dict[str, Tool] | None = None,
    ) -> ToolExecutionResult:
        self._raise_if_cancelled(context)
        if self._superseded_now(context, should_interrupt):
            return self._superseded(call)
        try:
            tool = self._registry.get(call.tool_name)
        except ToolNotFoundError as exc:
            if expected_tool_bindings is not None and call.tool_name in expected_tool_bindings:
                return self._changed_tool_result(call)
            return self._error(call, "tool_not_found", exc.user_message)
        # 先与模型请求发出时的工具对象比较；旧 schema 调用不能被交给
        # 响应期间刚接入的同名工具。审批和资源排队后还会再次核对。
        if (expected_tool_bindings is not None and call.tool_name in expected_tool_bindings
                and expected_tool_bindings[call.tool_name] is not tool):
            return self._changed_tool_result(call)
        if not tool_available_in_phase(tool.definition, context.work_phase):
            return self._error(call, "phase_disallowed", f"{call.tool_name} 在当前阶段不可用。")
        if available_tool_names is not None and call.tool_name not in available_tool_names:
            return self._error(call, "tool_not_found", f"{call.tool_name} 尚未加载。请先调用 tool_search，再在下一次模型请求中调用该工具。",
                               {"requires_tool_search": True})
        if expected_tool_bindings is not None and call.tool_name not in expected_tool_bindings:
            return self._changed_tool_result(call)
        argument_error = self._argument_error(call)
        if argument_error is not None:
            return argument_error
        check = self._permission_engine.evaluate(call=call, tool=tool.definition, context=context)
        approval_signature = self._permission_signature(check)
        if check.decision == "ask":
            await self._report_state(call, invocation, "awaiting_permission", on_invocation_state)
        denied = await self._handle_permission_check(call=call, tool_name=tool.definition.name,
                    permission_check=check, permission_resolver=permission_resolver,
                    should_interrupt=should_interrupt, context=context)
        if denied is not None:
            return denied
        self._raise_if_cancelled(context)
        if self._superseded_now(context, should_interrupt):
            return self._superseded(call)
        await self._report_state(call, invocation, "waiting_resources", on_invocation_state)
        async def report_waiting(snapshot: dict) -> None:
            await self._report_state(call, invocation, "waiting_resources", on_invocation_state, waiting=snapshot)
        management = (call.tool_name in {"process_stop", "process_read", "process_list", "process_background"}
                      and (tool.definition.permission is None or tool.definition.permission.source != "external"))
        context.resource_lease = await scheduler.reserve(claims, cancellation_token=context.cancellation_token,
            counted=not management, owner=ResourceOwner(context.session_id, invocation.invocation_id, call.tool_name),
            on_wait=report_waiting)
        self._raise_if_cancelled(context)
        if self._superseded_now(context, should_interrupt):
            return self._superseded(call)
        # 审批等待和资源排队都会让文件、规则发生变化；真正执行前再次检查。
        checked = self._permission_engine.evaluate(call=call, tool=tool.definition, context=context)
        if checked.decision == "deny":
            return self._permission_denied(call, checked)
        if checked.decision == "ask" and (check.decision != "ask" or self._permission_signature(checked) != approval_signature):
            return self._error(call, "permission_target_changed", "等待期间权限或目标已改变，请重新请求执行。")
        if self._resource_claims(call, context) != claims:
            return self._error(call, "resource_target_changed", "等待期间资源目标已改变，当前调用未执行。")
        argument_error = self._argument_error(call)
        if argument_error is not None:
            return argument_error
        if self._tool_replaced(call, tool):
            return self._changed_tool_result(call)
        if on_call_started is not None:
            await on_call_started(call)
        self._raise_if_cancelled(context)
        if self._superseded_now(context, should_interrupt):
            return self._superseded(call)
        await self._report_state(call, invocation, "running", on_invocation_state)
        # 状态通知也会让出控制权，停止或新输入可能就在此时到达。
        self._raise_if_cancelled(context)
        if self._superseded_now(context, should_interrupt):
            return self._superseded(call)
        try:
            # 进程工具分别管理本次等待期限和进程运行期限；普通工具超时不能
            # 把这些期限重新合并，也不能在停止流程完成之前打断收尾。
            # MCP 自己区分远端调用期限；执行器留出收尾时间，不用本地默认
            # 十秒截断服务器明确配置的长查询。
            server_timeout = getattr(tool, 'timeout_seconds', None)
            timeout = (None if call.tool_name in {"run_command", "process_wait", "process_stop", "process_background"}
                       else server_timeout + 1.0 if isinstance(server_timeout, (int, float)) and server_timeout > 0
                       else context.timeout_seconds)
            if self._tool_replaced(call, tool):
                return self._changed_tool_result(call)
            result = await self._await_cancelable(tool.execute(call.arguments, context), context, timeout=timeout)
            result.call_id, result.tool_name = call.call_id, call.tool_name
            return result
        except asyncio.CancelledError:
            if self._external_side_effect(tool):
                await self._report_state(call, invocation, "interrupted", on_invocation_state,
                                         error_code="mcp_outcome_unknown")
            raise
        except asyncio.TimeoutError:
            logger.error("event=tool_execution_timeout tool=%s", call.tool_name)
            if self._external_side_effect(tool):
                return self._unknown_remote_result(call, "等待远端结果超时")
            return self._error(call, "tool_timeout", f"{call.tool_name} 执行超时")
        except SessionRepositoryError:
            # 事件持久化失败意味着无法可靠审计后续执行，不能当作普通工具错误继续。
            raise
        except Exception as exc:
            logger.exception("event=tool_execution_failed tool=%s exception_type=%s", call.tool_name, type(exc).__name__)
            if self._external_side_effect(tool):
                return self._unknown_remote_result(call, "远端调用连接失败")
            return self._error(call, "tool_exception", str(exc))

    @staticmethod
    def _external_side_effect(tool: Tool) -> bool:
        # 执行事实属于已绑定的对象；目录撤销不能证明远端写入被撤销。
        definition = tool.definition
        return (definition.permission is not None and definition.permission.source == "external"
                and definition.category != "read")

    @staticmethod
    def _unknown_remote_result(call: ToolCall, reason: str) -> ToolExecutionResult:
        return ToolExecutor._error(call, "mcp_outcome_unknown",
            f"{reason}；远端操作可能已经发生。请先查询实际状态，再决定是否重试。",
            {"outcome_unknown": True, "automatic_retry": False})

    async def _handle_permission_check(
        self, *, call: ToolCall, tool_name: str, permission_check: PermissionCheck,
        permission_resolver: PermissionResolver | None, should_interrupt: Callable[[], bool] | None = None,
        context: ToolContext | None = None,
    ) -> ToolExecutionResult | None:
        if permission_check.decision == "allow":
            return None
        if permission_check.decision == "deny":
            return self._permission_denied(call, permission_check)
        request = permission_check.request
        if request is None or permission_resolver is None:
            return self._error(call, "permission_confirmation_unavailable", "当前工具调用需要用户授权，但没有可用的授权处理器。")
        resolution = await self._await_cancelable(permission_resolver(request), context)
        if context is not None:
            self._raise_if_cancelled(context)
            if self._superseded_now(context, should_interrupt):
                return self._superseded(call)
        if resolution.outcome == "superseded":
            return self._superseded(call)
        if resolution.request_id != request.request_id:
            return self._error(call, "permission_resolution_mismatch", "授权结果与当前请求不匹配。")
        if resolution.outcome in {"allow_once", "allow_session", "allow_project"}:
            self._permission_engine.apply_resolution(request, resolution)
            return None
        return self._error(call, "permission_user_denied", "用户拒绝了本次工具调用。",
                           {**(permission_check.metadata or {}), "permission_request_id": request.request_id})

    def _superseded_now(self, context: ToolContext, should_interrupt: Callable[[], bool] | None) -> bool:
        return ((should_interrupt is not None and should_interrupt())
                or not self.execution_runtime.is_current(context.session_id, context.generation))

    @staticmethod
    async def _await_cancelable(awaitable, context: ToolContext | None, *, timeout: float | None = None):
        operation = asyncio.ensure_future(awaitable)
        token = context.cancellation_token if context is not None else None
        cancellation_wait = asyncio.create_task(token.wait()) if token is not None else None
        try:
            if cancellation_wait is None:
                return await asyncio.wait_for(operation, timeout=timeout)
            done, _ = await asyncio.wait((operation, cancellation_wait), timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if operation.done():
                # 已提交的本地修改或已收到的远端结果保留真实结果；停止信号
                # 不能因为同时到达，就把已经成功的操作说成没有执行。
                return await operation
            if token.is_cancelled:
                raise asyncio.CancelledError
            if operation not in done:
                raise asyncio.TimeoutError
            return await operation
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if cancellation_wait is not None:
                cancellation_wait.cancel()
                await asyncio.gather(cancellation_wait, return_exceptions=True)

    @staticmethod
    def _permission_signature(check: PermissionCheck) -> tuple[object, ...]:
        request = check.request
        if request is None:
            return (check.decision,)
        return (request.tool_name, request.kind, request.command, request.details,
                tuple(request.file_paths), request.work_phase, request.permission_policy)

    @staticmethod
    def _permission_denied(call: ToolCall, check: PermissionCheck) -> ToolExecutionResult:
        return ToolExecutor._error(call, check.reason_code or "permission_denied",
                                  check.reason_message or "权限拒绝执行该工具调用。", check.metadata)

    @staticmethod
    def _error(call: ToolCall, code: str, message: str, metadata: dict[str, object] | None = None) -> ToolExecutionResult:
        return ToolExecutionResult(call.call_id, call.tool_name, content=message, is_error=True,
                                   summary=message, error_code=code, error_message=message, metadata=metadata or {})

    def _tool_replaced(self, call: ToolCall, tool) -> bool:
        try:
            return self._registry.get(call.tool_name) is not tool
        except ToolNotFoundError:
            return True

    @staticmethod
    def _changed_tool_result(call: ToolCall) -> ToolExecutionResult:
        return ToolExecutor._error(call, 'tool_changed', '等待期间工具目录已更新，当前调用未执行。请重新发现工具。',
                                   {'started': False, 'outcome': 'not_started'})

    @staticmethod
    def _superseded(call: ToolCall) -> ToolExecutionResult:
        return ToolExecutor._error(call, "steering_superseded", "用户已调整当前任务，此工具尚未执行。")

    @staticmethod
    def _raise_if_cancelled(context: ToolContext) -> None:
        if context.cancellation_token is not None and context.cancellation_token.is_cancelled:
            raise asyncio.CancelledError
