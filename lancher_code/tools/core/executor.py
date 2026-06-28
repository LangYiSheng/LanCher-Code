from __future__ import annotations

import asyncio
from pathlib import Path

from lancher_code.errors import ToolNotFoundError
from lancher_code.models import ToolCall, ToolContext, ToolExecutionResult
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
        self._context = ToolContext(
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            file_state_cache=FileStateCache(),
        )

    async def execute_calls(self, calls: list[ToolCall]) -> list[ToolExecutionResult]:
        results: list[ToolExecutionResult] = []
        safe_batch: list[ToolCall] = []

        for call in calls:
            try:
                tool = self._registry.get(call.tool_name)
            except ToolNotFoundError:
                if safe_batch:
                    results.extend(await self._execute_safe_batch(safe_batch))
                    safe_batch = []
                results.append(await self._execute_one(call))
                continue
            if tool.definition.is_concurrency_safe:
                safe_batch.append(call)
                continue

            if safe_batch:
                results.extend(await self._execute_safe_batch(safe_batch))
                safe_batch = []
            results.append(await self._execute_one(call))

        if safe_batch:
            results.extend(await self._execute_safe_batch(safe_batch))

        return results

    async def _execute_safe_batch(self, calls: list[ToolCall]) -> list[ToolExecutionResult]:
        return list(await asyncio.gather(*(self._execute_one(call) for call in calls)))

    async def _execute_one(self, call: ToolCall) -> ToolExecutionResult:
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
                tool.execute(call.arguments, self._context),
                timeout=self._context.timeout_seconds,
            )
            result.call_id = call.call_id
            result.tool_name = call.tool_name
            return result
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
