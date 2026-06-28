from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.models import ToolCall, ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import Tool
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry


class SuccessTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="success_tool", description="ok", input_schema={"type": "object"})

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id="",
            tool_name=self.definition.name,
            ok=True,
            payload={"content": f"{arguments['value']}@{context.cwd.name}"},
            summary="ok",
        )


class FailingTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="failing_tool", description="boom", input_schema={"type": "object"})

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        raise RuntimeError("boom")


class SlowTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="slow_tool", description="slow", input_schema={"type": "object"})

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        await asyncio.sleep(context.timeout_seconds + 0.1)
        return ToolExecutionResult(call_id="", tool_name=self.definition.name, ok=True, payload={}, summary="late")


def _call(name: str, arguments: dict[str, object], *, index: int = 0) -> ToolCall:
    return ToolCall(
        call_index=index,
        call_id=f"call-{index}",
        tool_name=name,
        arguments=arguments,
        arguments_json="{}",
    )


@pytest.mark.asyncio
async def test_executor_returns_success_result(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SuccessTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=0.5)

    results = await executor.execute_calls([_call("success_tool", {"value": "hello"})])

    assert results[0].ok is True
    assert results[0].call_id == "call-0"
    assert results[0].payload["content"] == f"hello@{tmp_path.name}"


@pytest.mark.asyncio
async def test_executor_wraps_missing_tool(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolRegistry(), cwd=tmp_path, timeout_seconds=0.5)

    results = await executor.execute_calls([_call("missing_tool", {})])

    assert results[0].ok is False
    assert results[0].error_code == "tool_not_found"


@pytest.mark.asyncio
async def test_executor_wraps_tool_exception(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=0.5)

    results = await executor.execute_calls([_call("failing_tool", {})])

    assert results[0].ok is False
    assert results[0].error_code == "tool_exception"
    assert results[0].error_message == "boom"


@pytest.mark.asyncio
async def test_executor_wraps_timeout(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SlowTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=0.01)

    results = await executor.execute_calls([_call("slow_tool", {})])

    assert results[0].ok is False
    assert results[0].error_code == "tool_timeout"


@pytest.mark.asyncio
async def test_executor_continues_after_failure(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FailingTool())
    registry.register(SuccessTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=0.5)

    results = await executor.execute_calls(
        [
            _call("failing_tool", {}, index=0),
            _call("success_tool", {"value": "done"}, index=1),
        ]
    )

    assert [result.ok for result in results] == [False, True]
    assert results[1].call_id == "call-1"
