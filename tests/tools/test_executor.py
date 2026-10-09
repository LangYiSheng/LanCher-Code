from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.models import (
    PermissionRequest,
    PermissionResolution,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolExecutionResult,
)
from lancher_code.permission_engine import PermissionEngine, PermissionStorage
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

    reported: list[ToolExecutionResult] = []

    async def on_result(result: ToolExecutionResult) -> None:
        reported.append(result)

    results = await executor.execute_calls([_call("failing_tool", {})], on_result=on_result)

    assert results[0].ok is False
    assert results[0].error_code == "tool_exception"
    assert results[0].error_message == "boom"
    assert reported == results


@pytest.mark.asyncio
async def test_executor_wraps_timeout(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SlowTool())
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=0.01)

    reported: list[ToolExecutionResult] = []

    async def on_result(result: ToolExecutionResult) -> None:
        reported.append(result)

    results = await executor.execute_calls([_call("slow_tool", {})], on_result=on_result)

    assert results[0].ok is False
    assert results[0].error_code == "tool_timeout"
    assert reported == results


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


class ControlledTool:
    """用事件控制工具进度，避免依赖执行速度断言并发顺序。"""

    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        self.entered.set()
        try:
            await self.release.wait()
            return ToolExecutionResult(call_id="", tool_name=self.definition.name, content="完成")
        finally:
            self.finished.set()


@pytest.mark.asyncio
async def test_result_callbacks_follow_completion_order_without_waiting_for_batch(tmp_path: Path) -> None:
    slow = ControlledTool(ToolDefinition(name="slow", description="", input_schema={}))
    fast = ControlledTool(ToolDefinition(name="fast", description="", input_schema={}))
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(fast)
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=5)
    reported: list[ToolExecutionResult] = []
    started: list[str] = []
    fast_reported = asyncio.Event()

    async def on_started(call: ToolCall) -> None:
        assert not {"slow": slow, "fast": fast}[call.tool_name].entered.is_set()
        started.append(call.call_id)

    async def on_result(result: ToolExecutionResult) -> None:
        reported.append(result)
        if result.tool_name == "fast":
            fast_reported.set()

    task = asyncio.create_task(executor.execute_calls(
        [_call("slow", {}, index=0), _call("fast", {}, index=1)],
        on_call_started=on_started, on_result=on_result,
    ))
    try:
        await asyncio.wait_for(slow.entered.wait(), timeout=1)
        await asyncio.wait_for(fast.entered.wait(), timeout=1)
        fast.release.set()
        await asyncio.wait_for(fast_reported.wait(), timeout=1)
        assert not task.done()
        assert [result.call_id for result in reported] == ["call-1"]
        slow.release.set()
        results = await asyncio.wait_for(task, timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert started == ["call-0", "call-1"]
    assert [result.call_id for result in reported] == ["call-1", "call-0"]
    assert [result.call_id for result in results] == ["call-0", "call-1"]
    assert results == list(reversed(reported))


@pytest.mark.asyncio
async def test_started_callback_waits_for_permission(tmp_path: Path) -> None:
    tool = ControlledTool(ToolDefinition(name="write_tool", description="", category="write"))
    tool.release.set()
    registry = ToolRegistry()
    registry.register(tool)
    waiting = asyncio.Event()
    approve = asyncio.Event()
    started: list[str] = []
    reported: list[ToolExecutionResult] = []

    async def resolver(request: PermissionRequest) -> PermissionResolution:
        waiting.set()
        await approve.wait()
        return PermissionResolution(request.request_id, "allow_once")

    async def on_started(call: ToolCall) -> None:
        assert approve.is_set()
        assert not tool.entered.is_set()
        started.append(call.call_id)

    async def on_result(result: ToolExecutionResult) -> None:
        reported.append(result)

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("write_tool", {})], permission_resolver=resolver,
        on_call_started=on_started, on_result=on_result,
    ))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert started == []
        assert reported == []
        assert not tool.entered.is_set()
        approve.set()
        results = await asyncio.wait_for(task, timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert started == ["call-0"]
    assert reported == results
    assert len(reported) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "expected_error"), [
    ("unknown", "tool_not_found"),
    ("unloaded", "tool_not_found"),
    ("phase", "phase_disallowed"),
    ("rule_denied", "permission_rule_deny"),
    ("user_denied", "permission_user_denied"),
    ("no_resolver", "permission_confirmation_unavailable"),
    ("approval_superseded", "steering_superseded"),
    ("interrupted", "steering_superseded"),
])
async def test_unexecuted_calls_report_once_without_started(
    tmp_path: Path, case: str, expected_error: str,
) -> None:
    tool = ControlledTool(ToolDefinition(name="write_tool", description="", category="write"))
    registry = ToolRegistry()
    registry.register(tool)
    storage = PermissionStorage()
    if case == "rule_denied":
        storage.add_session_rule("write_tool", "deny")
    executor = ToolExecutor(registry, cwd=tmp_path, permission_engine=PermissionEngine(storage))
    started: list[str] = []
    reported: list[ToolExecutionResult] = []

    async def on_started(call: ToolCall) -> None:
        started.append(call.call_id)

    async def on_result(result: ToolExecutionResult) -> None:
        reported.append(result)

    async def resolver(request: PermissionRequest) -> PermissionResolution:
        return PermissionResolution(request.request_id, "superseded" if case == "approval_superseded" else "deny")

    results = await executor.execute_calls(
        [_call("missing" if case == "unknown" else "write_tool", {})],
        work_phase="discuss" if case == "phase" else "execute",
        available_tool_names=set() if case == "unloaded" else None,
        permission_resolver=None if case == "no_resolver" else resolver,
        should_interrupt=lambda: case == "interrupted",
        on_call_started=on_started, on_result=on_result,
    )

    assert started == []
    assert not tool.entered.is_set()
    assert reported == results
    assert len(reported) == 1
    assert reported[0].error_code == expected_error


@pytest.mark.asyncio
async def test_interruption_after_batch_reports_remaining_calls_once(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SuccessTool())
    write = ControlledTool(ToolDefinition(name="write_tool", description="", category="write", is_concurrency_safe=False))
    registry.register(write)
    interrupt = False
    started: list[str] = []
    reported: list[ToolExecutionResult] = []

    async def on_started(call: ToolCall) -> None:
        started.append(call.call_id)

    async def on_result(result: ToolExecutionResult) -> None:
        nonlocal interrupt
        reported.append(result)
        interrupt = True

    results = await ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("success_tool", {"value": "ok"}), _call("write_tool", {}, index=1), _call("missing", {}, index=2)],
        should_interrupt=lambda: interrupt, on_call_started=on_started, on_result=on_result,
    )
    assert started == ["call-0"]
    assert [result.call_id for result in reported] == ["call-0", "call-1", "call-2"]
    assert reported == results
    assert [result.error_code for result in results] == [None, "steering_superseded", "steering_superseded"]
    assert not write.entered.is_set()


@pytest.mark.asyncio
async def test_cancelling_batch_does_not_report_unfinished_call(tmp_path: Path) -> None:
    slow = ControlledTool(ToolDefinition(name="slow", description=""))
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(SuccessTool())
    reported: list[ToolExecutionResult] = []
    fast_reported = asyncio.Event()

    async def on_result(result: ToolExecutionResult) -> None:
        reported.append(result)
        fast_reported.set()

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("slow", {}), _call("success_tool", {"value": "ok"}, index=1)], on_result=on_result,
    ))
    await asyncio.wait_for(slow.entered.wait(), timeout=1)
    await asyncio.wait_for(fast_reported.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [result.call_id for result in reported] == ["call-1"]
    assert slow.finished.is_set()


@pytest.mark.asyncio
async def test_cancelling_permission_wait_has_no_started_or_result_callback(tmp_path: Path) -> None:
    tool = ControlledTool(ToolDefinition(name="write_tool", description="", category="write"))
    registry = ToolRegistry()
    registry.register(tool)
    waiting = asyncio.Event()
    callbacks: list[str] = []

    async def resolver(request: PermissionRequest) -> PermissionResolution:
        waiting.set()
        await asyncio.Event().wait()
        return PermissionResolution(request.request_id, "allow_once")

    async def on_started(call: ToolCall) -> None:
        callbacks.append("started")

    async def on_result(result: ToolExecutionResult) -> None:
        callbacks.append("result")

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("write_tool", {})], permission_resolver=resolver,
        on_call_started=on_started, on_result=on_result,
    ))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert callbacks == []
    assert not tool.entered.is_set()


@pytest.mark.asyncio
async def test_callback_failure_cancels_other_running_tools_without_duplicate_reports(tmp_path: Path) -> None:
    slow = ControlledTool(ToolDefinition(name="slow", description=""))
    registry = ToolRegistry()
    registry.register(slow)
    registry.register(SuccessTool())
    reported: list[str] = []

    async def on_result(result: ToolExecutionResult) -> None:
        await slow.entered.wait()
        reported.append(result.call_id)
        raise RuntimeError("通知失败")

    with pytest.raises(RuntimeError, match="通知失败"):
        await ToolExecutor(registry, cwd=tmp_path).execute_calls(
            [_call("slow", {}), _call("success_tool", {"value": "ok"}, index=1)], on_result=on_result,
        )
    assert reported == ["call-1"]
    assert slow.finished.is_set()
