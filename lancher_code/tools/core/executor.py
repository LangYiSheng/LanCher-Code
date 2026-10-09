from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from lancher_code.errors import ToolNotFoundError
from lancher_code.logging_system import get_logger
from lancher_code.models import (
    CancellationToken,
    PermissionRequest,
    PermissionResolution,
    RuntimeMode,
    WorkPhase,
    PermissionPolicy,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    tool_available_in_phase,
)
from lancher_code.permission_engine import PermissionCheck, PermissionEngine
from lancher_code.tools.core.file_state_cache import FileStateCache
from lancher_code.tools.core.registry import ToolRegistry

logger = get_logger("tools.executor")

PermissionResolver = Callable[[PermissionRequest], Awaitable[PermissionResolution]]
ToolStartedCallback = Callable[[ToolCall], Awaitable[None]]
ToolResultCallback = Callable[[ToolExecutionResult], Awaitable[None]]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        cwd: Path,
        timeout_seconds: float = 10.0,
        permission_engine: PermissionEngine | None = None,
    ) -> None:
        self._registry = registry
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._file_state_cache = FileStateCache()
        self._session_id: str | None = None
        self._permission_engine = permission_engine or PermissionEngine()

    async def execute_calls(
        self,
        calls: list[ToolCall],
        *,
        mode: RuntimeMode = "default",
        work_phase: WorkPhase | None = None,
        permission_policy: PermissionPolicy | None = None,
        plan_file_path: Path | None = None,
        session_id: str | None = None,
        session_workspace: Path | None = None,
        session_root: Path | None = None,
        cancellation_token: CancellationToken | None = None,
        permission_resolver: PermissionResolver | None = None,
        available_tool_names: set[str] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
        on_call_started: ToolStartedCallback | None = None,
        on_result: ToolResultCallback | None = None,
    ) -> list[ToolExecutionResult]:
        if session_id != self._session_id:
            self._file_state_cache = FileStateCache()
            self._session_id = session_id
        context = ToolContext(
            cwd=self._cwd,
            timeout_seconds=self._timeout_seconds,
            mode=mode,
            work_phase=work_phase,
            permission_policy=permission_policy,
            project_root=self._cwd,
            plan_file_path=plan_file_path,
            session_id=session_id,
            session_workspace=session_workspace,
            session_root=session_root,
            cancellation_token=cancellation_token,
            file_state_cache=self._file_state_cache,
        )
        results: list[ToolExecutionResult] = []
        safe_batch: list[ToolCall] = []

        for index, call in enumerate(calls):
            self._raise_if_cancelled(context)
            if should_interrupt is not None and should_interrupt():
                results.extend(await self._skip_calls([*safe_batch, *calls[index:]], on_result))
                return results
            try:
                tool = self._registry.get(call.tool_name)
            except ToolNotFoundError:
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver, should_interrupt, on_call_started, on_result))
                    safe_batch = []
                results.append(await self._execute_one(call, context, permission_resolver, should_interrupt, on_call_started, on_result))
                continue

            if not tool_available_in_phase(tool.definition, context.work_phase):
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver, should_interrupt, on_call_started, on_result))
                    safe_batch = []
                results.append(
                    await self._report_result(ToolExecutionResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        content=f"{call.tool_name} 在当前模式下不可用。",
                        is_error=True,
                        metadata={"work_phase": context.work_phase, "permission_policy": context.permission_policy},
                        summary=f"{call.tool_name} 在当前模式下不可用",
                        error_code="phase_disallowed",
                        error_message=f"{call.tool_name} 在当前模式下不可用。",
                    ), on_result)
                )
                continue

            if available_tool_names is not None and call.tool_name not in available_tool_names:
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver, should_interrupt, on_call_started, on_result))
                    safe_batch = []
                if should_interrupt is not None and should_interrupt():
                    results.extend(await self._skip_calls(calls[index:], on_result))
                    return results
                results.append(
                    await self._report_result(ToolExecutionResult(
                        call_id=call.call_id, tool_name=call.tool_name,
                        content=f"{call.tool_name} 尚未加载。请先调用 tool_search，再在下一次模型请求中调用该工具。",
                        is_error=True, metadata={"requires_tool_search": True},
                        summary=f"{call.tool_name} 尚未加载", error_code="tool_not_found",
                        error_message=f"{call.tool_name} 尚未加载。",
                    ), on_result)
                )
                continue

            if tool.definition.is_concurrency_safe:
                safe_batch.append(call)
                continue

            if safe_batch:
                results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver, should_interrupt, on_call_started, on_result))
                safe_batch = []
            results.append(await self._execute_one(call, context, permission_resolver, should_interrupt, on_call_started, on_result))

        if safe_batch:
            results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver, should_interrupt, on_call_started, on_result))

        return results

    async def _execute_safe_batch(
        self,
        calls: list[ToolCall],
        context: ToolContext,
        permission_resolver: PermissionResolver | None,
        should_interrupt: Callable[[], bool] | None = None,
        on_call_started: ToolStartedCallback | None = None,
        on_result: ToolResultCallback | None = None,
    ) -> list[ToolExecutionResult]:
        if should_interrupt is not None and should_interrupt():
            return await self._skip_calls(calls, on_result)
        tasks = [
            asyncio.create_task(self._execute_one(call, context, permission_resolver, should_interrupt, on_call_started, on_result))
            for call in calls
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except (asyncio.CancelledError, Exception):
            # 回调失败也必须收束已启动的同组工具，不能留下后台操作。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_one(
        self,
        call: ToolCall,
        context: ToolContext,
        permission_resolver: PermissionResolver | None,
        should_interrupt: Callable[[], bool] | None = None,
        on_call_started: ToolStartedCallback | None = None,
        on_result: ToolResultCallback | None = None,
    ) -> ToolExecutionResult:
        # 每个调用完成时立即报告，不等待并发组中其他工具；返回值仍按输入排序。
        result = await self._run_one(call, context, permission_resolver, should_interrupt, on_call_started)
        return await self._report_result(result, on_result)

    async def _run_one(
        self,
        call: ToolCall,
        context: ToolContext,
        permission_resolver: PermissionResolver | None,
        should_interrupt: Callable[[], bool] | None = None,
        on_call_started: ToolStartedCallback | None = None,
    ) -> ToolExecutionResult:
        self._raise_if_cancelled(context)
        if should_interrupt is not None and should_interrupt():
            return self._superseded(call)
        try:
            tool = self._registry.get(call.tool_name)
        except ToolNotFoundError as exc:
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                content=exc.user_message,
                is_error=True,
                metadata={},
                summary=exc.user_message,
                error_code="tool_not_found",
                error_message=exc.user_message,
            )

        permission_check = self._permission_engine.evaluate(call=call, tool=tool.definition, context=context)
        maybe_denied = await self._handle_permission_check(
            call=call,
            tool_name=tool.definition.name,
            permission_check=permission_check,
            permission_resolver=permission_resolver,
            should_interrupt=should_interrupt,
            context=context,
        )
        if maybe_denied is not None:
            return maybe_denied

        self._raise_if_cancelled(context)
        if should_interrupt is not None and should_interrupt():
            return self._superseded(call)
        if on_call_started is not None:
            await on_call_started(call)
        self._raise_if_cancelled(context)

        try:
            result = await asyncio.wait_for(
                tool.execute(call.arguments, context),
                timeout=context.timeout_seconds,
            )
            result.call_id = call.call_id
            result.tool_name = call.tool_name
            return result
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error("event=tool_execution_timeout tool=%s", call.tool_name)
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                content=f"{call.tool_name} 执行超时",
                is_error=True,
                metadata={},
                summary=f"{call.tool_name} 执行超时",
                error_code="tool_timeout",
                error_message=f"{call.tool_name} 执行超时",
            )
        except Exception as exc:
            logger.exception(
                "event=tool_execution_failed tool=%s exception_type=%s",
                call.tool_name, type(exc).__name__,
            )
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                content=str(exc),
                is_error=True,
                metadata={},
                summary=f"{call.tool_name} 执行失败",
                error_code="tool_exception",
                error_message=str(exc),
            )

    async def _handle_permission_check(
        self,
        *,
        call: ToolCall,
        tool_name: str,
        permission_check: PermissionCheck,
        permission_resolver: PermissionResolver | None,
        should_interrupt: Callable[[], bool] | None = None,
        context: ToolContext | None = None,
    ) -> ToolExecutionResult | None:
        if permission_check.decision == "allow":
            return None

        metadata = permission_check.metadata or {}
        if permission_check.decision == "deny":
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=tool_name,
                content=permission_check.reason_message or "权限拒绝执行该工具调用。",
                is_error=True,
                metadata=metadata,
                summary="权限拒绝",
                error_code=permission_check.reason_code or "permission_denied",
                error_message=permission_check.reason_message or "权限拒绝执行该工具调用。",
            )

        request = permission_check.request
        if request is None or permission_resolver is None:
            return ToolExecutionResult(
                call_id=call.call_id,
                tool_name=tool_name,
                content="当前工具调用需要用户授权，但没有可用的授权处理器。",
                is_error=True,
                metadata=metadata,
                summary="缺少权限确认",
                error_code="permission_confirmation_unavailable",
                error_message="当前工具调用需要用户授权，但没有可用的授权处理器。",
            )

        resolution = await permission_resolver(request)
        if resolution.outcome == "superseded":
            return self._superseded(call)
        if context is not None:
            self._raise_if_cancelled(context)
        if should_interrupt is not None and should_interrupt():
            return self._superseded(call)
        self._permission_engine.apply_resolution(request, resolution)
        if resolution.outcome in {"allow_once", "allow_session", "allow_project"}:
            return None
        return ToolExecutionResult(
            call_id=call.call_id,
            tool_name=tool_name,
            content="用户拒绝了本次工具调用。",
            is_error=True,
            metadata={**metadata, "permission_request_id": request.request_id},
            summary="用户拒绝授权",
            error_code="permission_user_denied",
            error_message="用户拒绝了本次工具调用。",
        )

    @staticmethod
    async def _report_result(
        result: ToolExecutionResult,
        on_result: ToolResultCallback | None,
    ) -> ToolExecutionResult:
        if on_result is not None:
            await on_result(result)
        return result

    async def _skip_calls(
        self,
        calls: list[ToolCall],
        on_result: ToolResultCallback | None,
    ) -> list[ToolExecutionResult]:
        return [await self._report_result(self._superseded(call), on_result) for call in calls]

    @staticmethod
    def _superseded(call: ToolCall) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id=call.call_id, tool_name=call.tool_name, is_error=True,
            content="用户已调整当前任务，此工具尚未执行。", summary="已跳过待执行工具",
            error_code="steering_superseded", error_message="用户已调整当前任务，此工具尚未执行。",
        )

    @staticmethod
    def _raise_if_cancelled(context: ToolContext) -> None:
        if context.cancellation_token and context.cancellation_token.is_cancelled:
            raise asyncio.CancelledError
