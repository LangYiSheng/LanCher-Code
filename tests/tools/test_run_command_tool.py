from __future__ import annotations

import asyncio
import os
import shlex
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lancher_code.execution.contracts import ExecutionLimits, ResourceClaim
from lancher_code.execution.processes import ProcessSupervisor
from lancher_code.models import ToolContext
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.process import ProcessTool


def command(script):
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in (sys.executable, "-c", script))
    return shlex.join((sys.executable, "-c", script))


def context(root):
    supervisor = ProcessSupervisor(root, limits=ExecutionLimits(stop_grace_seconds=.05))
    runtime = SimpleNamespace(processes=supervisor, command_readiness=lambda command: None,
        command_claims=lambda command, cwd: (ResourceClaim("project", str(root)),))
    return ToolContext(cwd=root, timeout_seconds=1, execution_runtime=runtime,
                       session_id=uuid4().hex, invocation_id=uuid4().hex, turn_id="turn")


@pytest.mark.asyncio
async def test_run_command_captures_stdout_and_exit_code(tmp_path):
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute(
            {"description": "输出问候", "command": command("print('你好')"), "yield_ms": 5000}, ctx)
        assert result.ok and result.metadata["exit_code"] == 0
        assert "你好" in result.metadata["stdout"] and "描述: 输出问候" in result.content
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_run_command_nonzero_exit(tmp_path):
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute(
            {"description": "失败", "command": command("import sys;sys.exit(2)"), "yield_ms": 5000}, ctx)
        assert not result.ok and result.error_code == "non_zero_exit"
        assert result.metadata["exit_code"] == 2
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_run_command_yield_returns_managed_handle_and_process_tools(tmp_path):
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute(
            {"description": "后台", "command": command("print(input(),flush=True)"),
             "yield_ms": 0, "lifetime": "session"}, ctx)
        pid = result.metadata["process_id"]
        assert result.ok and result.metadata["status"] == "running"
        listed = await ProcessTool("process_list").execute({}, ctx)
        assert listed.metadata["processes"][0]["process_id"] == pid
        written = await ProcessTool("process_write").execute({"process_id": pid, "text": "hello\n"}, ctx)
        assert written.ok
        waited = await ProcessTool("process_wait").execute({"process_id": pid, "timeout_ms": 5000}, ctx)
        assert waited.ok and waited.metadata["exit_code"] == 0
        output = await ProcessTool("process_read").execute({"process_id": pid, "max_chars": 3}, ctx)
        assert output.ok and output.content == "hel" and output.metadata["next_cursor"] == 3
        stop = await ProcessTool("process_stop").execute({"process_id": pid}, ctx)
        assert stop.ok
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_process_tools_reject_another_session_process(tmp_path):
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute(
            {"description": "后台", "command": command("import time;time.sleep(30)"), "yield_ms": 0}, ctx)
        other = context(tmp_path)
        other.execution_runtime = ctx.execution_runtime
        rejected = await ProcessTool("process_stop").execute({"process_id": result.metadata["process_id"]}, other)
        assert not rejected.ok
        assert ctx.execution_runtime.processes.get(result.metadata["process_id"], ctx.session_id).status == "running"
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.parametrize("arguments", [
    {"command": "x"}, {"description": " ", "command": "x"},
    {"description": "x", "command": ""}, {"description": "x", "command": "x", "yield_ms": True},
    {"description": "x", "command": "x", "max_runtime_ms": -1},
    {"description": "x", "command": "x", "lifetime": "forever"},
])
@pytest.mark.asyncio
async def test_run_command_invalid_arguments(tmp_path, arguments):
    ctx = context(tmp_path)
    result = await RunCommandTool().execute(arguments, ctx)
    assert not result.ok and result.error_code == "invalid_arguments"
    assert ctx.execution_runtime.processes.list(ctx.session_id) == []
    await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_run_command_requires_session_and_execute_phase(tmp_path):
    result = await RunCommandTool().execute({"description": "x", "command": "x"},
                                           ToolContext(cwd=tmp_path, timeout_seconds=1))
    assert not result.ok
    ctx = context(tmp_path)
    ctx.work_phase = "plan"
    result = await RunCommandTool().execute({"description": "x", "command": "x"}, ctx)
    assert not result.ok and not ctx.execution_runtime.processes.active_session(ctx.session_id)
    await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_run_command_propagates_journal_failure_before_spawn(tmp_path):
    from lancher_code.sessions.repository import SessionRepositoryError
    ctx = context(tmp_path)
    def fail(*args, **kwargs):
        raise SessionRepositoryError("journal failed")
    ctx.execution_runtime.processes.event_sink = fail
    try:
        with pytest.raises(SessionRepositoryError):
            await RunCommandTool().execute({"description": "失败", "command": command("print(1)")}, ctx)
        assert not ctx.execution_runtime.processes.active_session(ctx.session_id)
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_process_background_propagates_journal_failure_and_stops_target(tmp_path):
    from lancher_code.sessions.repository import SessionRepositoryError
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute({"description": "后台", "command": command("import time;time.sleep(30)"),
                                              "yield_ms": 0}, ctx)
        pid = result.metadata["process_id"]
        def fail(session_id, kind, data, **kwargs):
            if kind == "process.backgrounded":
                raise SessionRepositoryError("journal failed")
        ctx.execution_runtime.processes.event_sink = fail
        with pytest.raises(SessionRepositoryError):
            await ProcessTool("process_background").execute({"process_id": pid}, ctx)
        info = await ctx.execution_runtime.processes.wait(pid, ctx.session_id, timeout_ms=5000)
        assert info.status == "failed" and info.exit_reason == "event_error"
    finally:
        await ctx.execution_runtime.processes.close()
