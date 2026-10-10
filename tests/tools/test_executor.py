from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.permissions.models import PermissionRequest, PermissionResolution
from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolExecutionResult, ToolPermissionMetadata
from lancher_code.tools.context import ToolContext
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.execution.contracts import ResourceClaim, ResourceOwner
from lancher_code.execution.contracts import ExecutionConfig, ExecutionLimits
from lancher_code.execution.runtime import ExecutionRuntime
from lancher_code.execution.scheduler import get_project_scheduler, path_claim, project_claim
from lancher_code.contracts.control import CancellationToken
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry


class SuccessTool:
    def resource_claims(self, arguments, context):
        return (ResourceClaim("external", "test:success", "shared"),)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="success_tool", description="ok", input_schema={"type": "object"})

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        return ToolExecutionResult(
            call_id="",
            tool_name=self.definition.name,
            is_error=False,
            content=f"{arguments['value']}@{context.cwd.name}", metadata={},
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
        return ToolExecutionResult(call_id="", tool_name=self.definition.name, is_error=False, metadata={}, summary="late")


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

    assert (not results[0].is_error) is True
    assert results[0].call_id == "call-0"
    assert results[0].content == f"hello@{tmp_path.name}"


@pytest.mark.asyncio
async def test_executor_wraps_missing_tool(tmp_path: Path) -> None:
    executor = ToolExecutor(ToolRegistry(), cwd=tmp_path, timeout_seconds=0.5)

    results = await executor.execute_calls([_call("missing_tool", {})])

    assert (not results[0].is_error) is False
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

    assert (not results[0].is_error) is False
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

    assert (not results[0].is_error) is False
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

    assert [(not result.is_error) for result in results] == [False, True]
    assert results[1].call_id == "call-1"


class ControlledTool:
    """用事件控制工具进度，避免依赖执行速度断言并发顺序。"""

    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    def resource_claims(self, arguments, context):
        if self.definition.category == "write":
            from lancher_code.execution.scheduler import project_claim
            return (project_claim(context.project_root),)
        return (ResourceClaim("external", "test:" + self.definition.name, "shared"),)

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
    write = ControlledTool(ToolDefinition(name="write_tool", description="", category="write"))
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


class FileControlledTool(ControlledTool):
    def __init__(self, *, writing: bool = True) -> None:
        super().__init__(ToolDefinition(name="write_file" if writing else "read_file", description="",
                                        category="write" if writing else "read"))
        self.started_paths: list[str] = []

    def resource_claims(self, arguments, context):
        return (path_claim(context.cwd / str(arguments["path"]), write=self.definition.category == "write"),)

    async def execute(self, arguments, context):
        self.started_paths.append(str(arguments["path"]))
        return await super().execute(arguments, context)


@pytest.mark.asyncio
async def test_default_background_command_releases_fallback_after_returning_handle(tmp_path: Path) -> None:
    class BackgroundCommand:
        definition = ToolDefinition("run_command", "", category="command")
        lease = None

        async def execute(self, arguments, context):
            self.lease = context.resource_lease
            self.lease.transfer()
            self.lease.bind_process("server")
            return ToolExecutionResult("", "run_command", content="后台句柄", metadata={"process_id": "server"})

    command = BackgroundCommand()
    file = FileControlledTool()
    file.release.set()
    registry = ToolRegistry()
    registry.register(command)
    registry.register(file)
    executor = ToolExecutor(registry, cwd=tmp_path)
    result, = await executor.execute_calls([_call("run_command", {"command": "python server.py",
        "description": "启动服务", "lifetime": "session"})], permission_policy="bypass")
    assert (not result.is_error) and command.lease.transferred
    try:
        assert (not (await asyncio.wait_for(executor.execute_calls([
            _call("write_file", {"path": "client.py", "content": "请求服务"})], permission_policy="bypass"), 1))[0].is_error)
        assert command.lease.released
    finally:
        await command.lease.release()


@pytest.mark.asyncio
async def test_approved_call_reports_real_waiting_resource_and_clears_it_at_start(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = FileControlledTool()
    tool.release.set()
    registry.register(tool)
    scheduler = get_project_scheduler(tmp_path)
    owner = await scheduler.reserve([project_claim(tmp_path)],
        owner=ResourceOwner("background-session", "background-invocation", "run_command", "server"))
    updates = asyncio.Queue()

    async def on_state(call, invocation):
        await updates.put(invocation.to_dict())

    async def resolver(request):
        return PermissionResolution(request.request_id, "allow_once")

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("write_file", {"path": "a", "content": "new"})], permission_resolver=resolver,
        on_invocation_state=on_state))
    try:
        states = []
        while True:
            update = await asyncio.wait_for(updates.get(), 1)
            states.append(update["state"])
            if update["waiting"].get("blockers"):
                break
        assert "awaiting_permission" in states and update["state"] == "waiting_resources"
        assert update["waiting"]["blockers"][0]["process_id"] == "server"
        assert not tool.entered.is_set()
        await owner.release()
        assert (not (await asyncio.wait_for(task, 1))[0].is_error)
        remaining = []
        while not updates.empty():
            remaining.append(updates.get_nowait())
        assert [item["state"] for item in remaining] == ["running", "succeeded"]
        assert all(item["waiting"] == {} for item in remaining)
    finally:
        await owner.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_batch_predecessor_is_queued_before_permission_or_resource_wait(tmp_path: Path) -> None:
    tool = FileControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    updates = asyncio.Queue()

    async def on_state(call, invocation):
        await updates.put((call.call_id, invocation.to_dict()))

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls([
        _call("write_file", {"path": "a", "content": "first"}),
        _call("write_file", {"path": "a", "content": "second"}, index=1)],
        permission_policy="bypass", on_invocation_state=on_state))
    try:
        await asyncio.wait_for(tool.entered.wait(), 1)
        before = []
        while not updates.empty():
            before.append(updates.get_nowait())
        second = [item for call_id, item in before if call_id == "call-1"]
        assert len(second) == 1 and second[0]["state"] == "queued"
        assert second[0]["waiting"]["reason"] == "predecessors"
        assert second[0]["waiting"]["blockers"][0]["invocation_id"]
        tool.release.set()
        assert all((not result.is_error) for result in await asyncio.wait_for(task, 1))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_arriving_in_running_state_callback_prevents_real_execution(tmp_path: Path) -> None:
    tool = FileControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    token = CancellationToken()

    async def on_state(call, invocation):
        if invocation.state == "running":
            token.cancel()

    with pytest.raises(asyncio.CancelledError):
        await ToolExecutor(registry, cwd=tmp_path).execute_calls([
            _call("write_file", {"path": "a", "content": "不能执行"})],
            permission_policy="bypass", cancellation_token=token, on_invocation_state=on_state)
    assert not tool.entered.is_set()
    scheduler = get_project_scheduler(tmp_path)
    assert scheduler.active_count == scheduler.waiting_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_index", [0, 1])
async def test_completed_predecessor_disappears_while_other_predecessor_still_runs(
    tmp_path: Path, completed_index: int,
) -> None:
    releases = {name: asyncio.Event() for name in ["first", "second", "third"]}
    entered = {name: asyncio.Event() for name in releases}
    queued = asyncio.Queue()

    class MultiResourceTool:
        definition = ToolDefinition("write_file", "", category="write")

        def resource_claims(self, arguments, context):
            return tuple(path_claim(context.cwd / path, write=True) for path in arguments["locks"])

        async def execute(self, arguments, context):
            name = arguments["label"]
            entered[name].set()
            await releases[name].wait()
            return ToolExecutionResult("", "write_file", content=name)

    async def on_state(call, invocation):
        if call.call_id == "call-2" and invocation.state == "queued":
            await queued.put(invocation.to_dict())

    registry = ToolRegistry()
    registry.register(MultiResourceTool())
    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls([
        _call("write_file", {"label": "first", "locks": ["a"], "path": "a", "content": "a"}),
        _call("write_file", {"label": "second", "locks": ["b"], "path": "b", "content": "b"}, index=1),
        _call("write_file", {"label": "third", "locks": ["a", "b"], "path": "a", "content": "c"}, index=2)],
        permission_policy="bypass", on_invocation_state=on_state))
    try:
        await asyncio.wait_for(entered["first"].wait(), 1)
        await asyncio.wait_for(entered["second"].wait(), 1)
        before = await asyncio.wait_for(queued.get(), 1)
        assert len(before["waiting"]["blockers"]) == 2
        ids = [item["invocation_id"] for item in before["waiting"]["blockers"]]
        names = ["first", "second"]
        releases[names[completed_index]].set()
        after = await asyncio.wait_for(queued.get(), 1)
        assert [item["invocation_id"] for item in after["waiting"]["blockers"]] == [ids[1 - completed_index]]
        assert ids[0] != ids[1] and not entered["third"].is_set()
        releases[names[1 - completed_index]].set()
        await asyncio.wait_for(entered["third"].wait(), 1)
        releases["third"].set()
        assert all((not result.is_error) for result in await asyncio.wait_for(task, 1))
    finally:
        for event in releases.values():
            event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_independent_file_writes_start_in_parallel(tmp_path: Path) -> None:
    tool = FileControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("write_file", {"path": "a", "content": "a"}),
         _call("write_file", {"path": "b", "content": "b"}, index=1)], permission_policy="bypass"))
    try:
        for _ in range(40):
            if len(tool.started_paths) == 2:
                break
            await asyncio.sleep(0)
        assert tool.started_paths == ["a", "b"]
        tool.release.set()
        assert len(await task) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_permission_does_not_hold_resources(tmp_path: Path) -> None:
    write, read = FileControlledTool(), FileControlledTool(writing=False)
    read.release.set()
    first, second = ToolRegistry(), ToolRegistry()
    first.register(write)
    second.register(read)
    awaiting_approval, approve = asyncio.Event(), asyncio.Event()

    async def resolver(request):
        awaiting_approval.set()
        await approve.wait()
        return PermissionResolution(request.request_id, "allow_once")

    task = asyncio.create_task(ToolExecutor(first, cwd=tmp_path).execute_calls(
        [_call("write_file", {"path": "a", "content": "new"})], permission_resolver=resolver))
    try:
        await asyncio.wait_for(awaiting_approval.wait(), 1)
        result = await asyncio.wait_for(ToolExecutor(second, cwd=tmp_path).execute_calls([_call("read_file", {"path": "a"})]), 1)
        assert (not result[0].is_error) and not write.entered.is_set()
        approve.set()
        write.release.set()
        assert (not (await task)[0].is_error)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resource_wait_rechecks_changed_permission_before_start(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = FileControlledTool()
    registry.register(tool)
    storage = PermissionStorage()
    scheduler = get_project_scheduler(tmp_path)
    owner = await scheduler.reserve([project_claim(tmp_path)])
    approved = asyncio.Event()

    async def resolver(request):
        approved.set()
        return PermissionResolution(request.request_id, "allow_once")

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("write_file", {"path": "a", "content": "new"})], permission_resolver=resolver))
    try:
        await asyncio.wait_for(approved.wait(), 1)
        for _ in range(20):
            if scheduler.waiting_count:
                break
            await asyncio.sleep(0)
        storage.add_session_rule("WriteFile(a)", "deny")
        await owner.release()
        assert (await asyncio.wait_for(task, 1))[0].error_code == "permission_rule_deny"
        assert not tool.entered.is_set()
    finally:
        await owner.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_generation_approval_does_not_persist_grant(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = FileControlledTool()
    registry.register(tool)
    storage = PermissionStorage()
    runtime = ExecutionRuntime(tmp_path)

    async def resolver(request):
        runtime.invalidate("session")
        return PermissionResolution(request.request_id, "allow_session")

    result = await ToolExecutor(registry, cwd=tmp_path, permission_engine=PermissionEngine(storage),
                                execution_runtime=runtime).execute_calls(
        [_call("write_file", {"path": "a", "content": "new"})], session_id="session", permission_resolver=resolver)
    assert result[0].error_code == "steering_superseded"
    assert storage.rules_for_scope("session") == []
    assert not tool.entered.is_set()


@pytest.mark.asyncio
async def test_token_cancel_waiting_resources_never_starts_tool(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = FileControlledTool()
    registry.register(tool)
    token = CancellationToken()
    scheduler = get_project_scheduler(tmp_path)
    owner = await scheduler.reserve([project_claim(tmp_path)])
    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("write_file", {"path": "a", "content": "new"})], permission_policy="bypass", cancellation_token=token))
    try:
        for _ in range(40):
            if scheduler.waiting_count:
                break
            await asyncio.sleep(0)
        assert scheduler.waiting_count == 1
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not tool.entered.is_set() and scheduler.waiting_count == 0
    finally:
        await owner.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_write_result_survives_simultaneous_token_stop(tmp_path: Path) -> None:
    class CommittingTool(SuccessTool):
        async def execute(self, arguments, context):
            context.cancellation_token.cancel()
            return ToolExecutionResult("", self.definition.name, content="committed")

    registry = ToolRegistry()
    registry.register(CommittingTool())
    result = await ToolExecutor(registry, cwd=tmp_path).execute_calls(
        [_call("success_tool", {"value": "ok"})], cancellation_token=CancellationToken())
    assert (not result[0].is_error) and result[0].content == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking_tool", ["process_wait", "process_write"])
async def test_stop_process_enters_when_ordinary_capacity_is_full(tmp_path: Path, blocking_tool: str) -> None:
    blocked = ControlledTool(ToolDefinition(blocking_tool, "", category="read" if blocking_tool == "process_wait" else "command"))
    stop_entered = asyncio.Event()

    class StopTool:
        definition = ToolDefinition("process_stop", "", category="command")

        def resource_claims(self, arguments, context):
            return ()

        async def execute(self, arguments, context):
            await blocked.entered.wait()
            stop_entered.set()
            blocked.release.set()
            return ToolExecutionResult("", "process_stop", content="stopped")

    registry = ToolRegistry()
    registry.register(blocked)
    registry.register(StopTool())
    runtime = ExecutionRuntime(tmp_path, ExecutionConfig(limits=ExecutionLimits(max_concurrency=1)))
    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path, execution_runtime=runtime).execute_calls([
        _call(blocking_tool, {"process_id": "a" * 32}), _call("process_stop", {"process_id": "a" * 32}, index=1)],
        permission_policy="bypass"))
    try:
        await asyncio.wait_for(stop_entered.wait(), 1)
        results = await asyncio.wait_for(task, 1)
        assert all((not result.is_error) for result in results)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class RemoteControlledTool(ControlledTool):
    def __init__(self, *, read_only: bool = False):
        super().__init__(ToolDefinition("mcp__test__operation", "", category="read" if read_only else "command",
            permission=ToolPermissionMetadata("external", "mcp__test__operation", "远端操作")))


@pytest.mark.asyncio
async def test_remote_cancel_during_running_notification_is_not_unknown_outcome(tmp_path: Path) -> None:
    tool = RemoteControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    runtime = ExecutionRuntime(tmp_path)
    token = CancellationToken()
    states = []

    async def on_state(call, invocation):
        states.append(invocation.to_dict())
        if invocation.state == "running":
            token.cancel()

    with pytest.raises(asyncio.CancelledError):
        await ToolExecutor(registry, cwd=tmp_path, execution_runtime=runtime).execute_calls([
            _call(tool.definition.name, {})], permission_policy="bypass",
            cancellation_token=token, on_invocation_state=on_state)
    assert not tool.entered.is_set()
    assert states[-1]["state"] == "cancelled" and states[-1]["error_code"] is None
    assert runtime.list_invocations(None)[0]["state"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [True, False])
async def test_running_remote_cancel_records_unknown_only_for_side_effects(tmp_path: Path, read_only: bool) -> None:
    tool = RemoteControlledTool(read_only=read_only)
    registry = ToolRegistry()
    registry.register(tool)
    runtime = ExecutionRuntime(tmp_path)
    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path, execution_runtime=runtime).execute_calls(
        [_call(tool.definition.name, {})], permission_policy="bypass"))
    await asyncio.wait_for(tool.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    info = runtime.list_invocations(None)[0]
    assert info["state"] == ("cancelled" if read_only else "interrupted")
    assert info["error_code"] == (None if read_only else "mcp_outcome_unknown")


@pytest.mark.asyncio
async def test_remote_cancel_before_approval_is_known_not_started(tmp_path: Path) -> None:
    tool = RemoteControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    runtime = ExecutionRuntime(tmp_path)
    waiting = asyncio.Event()

    async def resolver(request):
        waiting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(ToolExecutor(registry, cwd=tmp_path, execution_runtime=runtime).execute_calls(
        [_call(tool.definition.name, {})], permission_resolver=resolver))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not tool.entered.is_set()
    info = runtime.list_invocations(None)[0]
    assert info["state"] == "cancelled" and info["error_code"] is None


@pytest.mark.asyncio
async def test_remote_timeout_returns_outcome_unknown_without_retry(tmp_path: Path) -> None:
    tool = RemoteControlledTool()
    registry = ToolRegistry()
    registry.register(tool)
    runtime = ExecutionRuntime(tmp_path)
    result = await ToolExecutor(registry, cwd=tmp_path, execution_runtime=runtime, timeout_seconds=0.01).execute_calls(
        [_call(tool.definition.name, {})], permission_policy="bypass")
    assert result[0].error_code == "mcp_outcome_unknown"
    assert result[0].metadata["automatic_retry"] is False
    assert "查询实际状态" in result[0].content
    assert runtime.list_invocations(None)[0]["state"] == "interrupted"
