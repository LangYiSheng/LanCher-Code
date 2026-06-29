from __future__ import annotations

import asyncio
from pathlib import Path

from lancher_code.errors import ToolNotFoundError
from lancher_code.models import CancellationToken, RuntimeMode, ToolCall, ToolContext, ToolExecutionResult
from lancher_code.tools.core.file_state_cache import FileStateCache
from lancher_code.tools.core.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        cwd: Path,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._registry = registry
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._file_state_cache = FileStateCache()

    async def execute_calls(
        self,
        calls: list[ToolCall],
        *,
        mode: RuntimeMode = "normal",
        plan_file_path: Path | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> list[ToolExecutionResult]:
        context = ToolContext(
            cwd=self._cwd,
            timeout_seconds=self._timeout_seconds,
            mode=mode,
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
                    results.extend(await self._execute_safe_batch(safe_batch, context))
                    safe_batch = []
                results.append(await self._execute_one(call, context))
                continue

            if mode not in tool.definition.allowed_modes:
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch, context))
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
                results.extend(await self._execute_safe_batch(safe_batch, context))
                safe_batch = []
            results.append(await self._execute_one(call, context))

        if safe_batch:
            results.extend(await self._execute_safe_batch(safe_batch, context))

        return results

    async def _execute_safe_batch(self, calls: list[ToolCall], context: ToolContext) -> list[ToolExecutionResult]:
        tasks = [asyncio.create_task(self._execute_one(call, context)) for call in calls]
        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_one(self, call: ToolCall, context: ToolContext) -> ToolExecutionResult:
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

    @staticmethod
    def _raise_if_cancelled(context: ToolContext) -> None:
        if context.cancellation_token and context.cancellation_token.is_cancelled:
            raise asyncio.CancelledError
