from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from lancher_code.errors import ToolNotFoundError
from lancher_code.models import (
    CancellationToken,
    PermissionRequest,
    PermissionResolution,
    RuntimeMode,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
)
from lancher_code.permission_engine import PermissionCheck, PermissionEngine
from lancher_code.tools.core.file_state_cache import FileStateCache
from lancher_code.tools.core.registry import ToolRegistry

PermissionResolver = Callable[[PermissionRequest], Awaitable[PermissionResolution]]


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
        self._permission_engine = permission_engine or PermissionEngine()

    async def execute_calls(
        self,
        calls: list[ToolCall],
        *,
        mode: RuntimeMode = "default",
        plan_file_path: Path | None = None,
        cancellation_token: CancellationToken | None = None,
        permission_resolver: PermissionResolver | None = None,
    ) -> list[ToolExecutionResult]:
        context = ToolContext(
            cwd=self._cwd,
            timeout_seconds=self._timeout_seconds,
            mode=mode,
            project_root=self._cwd,
            plan_file_path=plan_file_path,
            cancellation_token=cancellation_token,
            file_state_cache=self._file_state_cache,
        )
        results: list[ToolExecutionResult] = []
        safe_batch: list[ToolCall] = []

        for call in calls:
            self._raise_if_cancelled(context)
            try:
                tool = self._registry.get(call.tool_name)
            except ToolNotFoundError:
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver))
                    safe_batch = []
                results.append(await self._execute_one(call, context, permission_resolver))
                continue

            if mode not in tool.definition.allowed_modes:
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver))
                    safe_batch = []
                results.append(
                    ToolExecutionResult(
                        call_id=call.call_id,
                        tool_name=call.tool_name,
                        content=f"{call.tool_name} 在当前模式下不可用。",
                        is_error=True,
                        metadata={"mode": mode},
                        summary=f"{call.tool_name} 在当前模式下不可用",
                        error_code="mode_disallowed",
                        error_message=f"{call.tool_name} 在当前模式下不可用。",
                    )
                )
                continue

            if tool.definition.is_concurrency_safe:
                safe_batch.append(call)
                continue

            if safe_batch:
                results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver))
                safe_batch = []
            results.append(await self._execute_one(call, context, permission_resolver))

        if safe_batch:
            results.extend(await self._execute_safe_batch(safe_batch, context, permission_resolver))

        return results

    async def _execute_safe_batch(
        self,
        calls: list[ToolCall],
        context: ToolContext,
        permission_resolver: PermissionResolver | None,
    ) -> list[ToolExecutionResult]:
        tasks = [asyncio.create_task(self._execute_one(call, context, permission_resolver)) for call in calls]
        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_one(
        self,
        call: ToolCall,
        context: ToolContext,
        permission_resolver: PermissionResolver | None,
    ) -> ToolExecutionResult:
        self._raise_if_cancelled(context)
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
        )
        if maybe_denied is not None:
            return maybe_denied

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
    def _raise_if_cancelled(context: ToolContext) -> None:
        if context.cancellation_token and context.cancellation_token.is_cancelled:
            raise asyncio.CancelledError
