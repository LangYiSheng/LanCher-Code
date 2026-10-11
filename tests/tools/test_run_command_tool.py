from __future__ import annotations

from lancher_code.tools.core.file_state_cache import FileStateCache

import asyncio
import os
import shlex
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lancher_code.execution.contracts import ExecutionLimits, ResourceClaim
from lancher_code.execution.processes import ProcessSupervisor
from lancher_code.tools.context import ToolContext
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
    return ToolContext(file_state_cache=FileStateCache(), cwd=root, timeout_seconds=1, execution_runtime=runtime,
                       session_id=uuid4().hex, invocation_id=uuid4().hex, turn_id="turn")


@pytest.mark.asyncio
async def test_run_command_captures_stdout_and_exit_code(tmp_path):
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute(
            {"description": "输出问候", "command": command("print('你好')"), "yield_ms": 5000}, ctx)
        assert (not result.is_error) and result.metadata["exit_code"] == 0
        assert "你好" in result.metadata["stdout"] and "描述: 输出问候" in result.content
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_run_command_nonzero_exit(tmp_path):
    ctx = context(tmp_path)
    try:
        result = await RunCommandTool().execute(
            {"description": "失败", "command": command("import sys;sys.exit(2)"), "yield_ms": 5000}, ctx)
        assert not (not result.is_error) and result.error_code == "non_zero_exit"
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
        assert (not result.is_error) and result.metadata["status"] == "running"
        listed = await ProcessTool("process_list").execute({}, ctx)
        assert listed.metadata["processes"][0]["process_id"] == pid
        written = await ProcessTool("process_write").execute({"process_id": pid, "text": "hello\n"}, ctx)
        assert (not written.is_error)
        waited = await ProcessTool("process_wait").execute({"process_id": pid, "timeout_ms": 5000}, ctx)
        assert (not waited.is_error) and waited.metadata["exit_code"] == 0
        output = await ProcessTool("process_read").execute({"process_id": pid, "max_chars": 3}, ctx)
        assert (not output.is_error) and output.content == "hel" and output.metadata["next_cursor"] == 3
        stop = await ProcessTool("process_stop").execute({"process_id": pid}, ctx)
        assert (not stop.is_error)
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
        assert not (not rejected.is_error)
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
    assert not (not result.is_error) and result.error_code == "invalid_arguments"
    assert ctx.execution_runtime.processes.list(ctx.session_id) == []
    await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_run_command_requires_session_and_obeys_readonly_phase(tmp_path):
    result = await RunCommandTool().execute({"description": "x", "command": "x"},
                                           ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1))
    assert not (not result.is_error)
    ctx = context(tmp_path)
    ctx.work_phase = "plan"
    result = await RunCommandTool().execute({"description": "x", "command": "x"}, ctx)
    assert not (not result.is_error) and not ctx.execution_runtime.processes.active_session(ctx.session_id)
    await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
async def test_authorized_command_uses_external_cwd(tmp_path):
    project, external = tmp_path / "project", tmp_path / "external"
    project.mkdir()
    external.mkdir()
    ctx = context(project)
    try:
        result = await RunCommandTool().execute({
            "description": "核对实际工作目录", "command": command("from pathlib import Path;print(Path.cwd())"),
            "cwd": str(external), "yield_ms": 5000}, ctx)
        assert not result.is_error, result.content
        assert result.metadata["cwd"] == str(external.resolve())
        assert str(external.resolve()) in result.metadata["stdout"]
    finally:
        await ctx.execution_runtime.processes.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discuss", "plan"])
async def test_readonly_command_runs_but_unknown_script_is_refused(tmp_path, phase):
    ctx = context(tmp_path)
    ctx.work_phase = phase
    try:
        result = await RunCommandTool().execute({"description": "查看目录", "command": (
            "Get-Location" if os.name == "nt" else "pwd"), "yield_ms": 5000}, ctx)
        assert not result.is_error, result.content
        unknown = await RunCommandTool().execute({"description": "运行脚本", "command": command("print('query')")}, ctx)
        assert unknown.error_code == "phase_disallowed"
    finally:
        await ctx.execution_runtime.processes.close()


def test_command_resource_claims_cover_known_external_file(tmp_path):
    project, external = tmp_path / "project", tmp_path / "external"
    project.mkdir()
    external.mkdir()
    file = external / "note.txt"
    file.write_text("note", encoding="utf-8")
    ctx = context(project)
    quoted = "'" + str(file).replace("'", "''") + "'" if os.name == "nt" else shlex.quote(str(file))
    command_text = ("Get-Content " if os.name == "nt" else "cat ") + quoted
    claims = RunCommandTool().resource_claims({"command": command_text}, ctx)
    assert any(claim.kind == "path" and __import__("pathlib").Path(claim.key).resolve() == file.resolve()
               and claim.mode == "shared" for claim in claims)


def test_empty_command_profile_cannot_remove_actual_external_cwd_claim(tmp_path):
    project, external = tmp_path / "project", tmp_path / "external"
    project.mkdir()
    external.mkdir()
    ctx = context(project)
    ctx.execution_runtime.command_claims = lambda command_text, cwd: ()
    claims = RunCommandTool().resource_claims({"command": "Get-Location" if os.name == "nt" else "pwd",
                                              "cwd": str(external)}, ctx)
    assert any(claim.kind == "path" and __import__("pathlib").Path(claim.key).resolve() == external.resolve()
               and claim.recursive and claim.mode == "shared" and claim.lifetime == "invocation" for claim in claims)


@pytest.mark.asyncio
async def test_run_command_propagates_journal_failure_before_spawn(tmp_path):
    from lancher_code.sessions.storage import SessionRepositoryError
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
    from lancher_code.sessions.storage import SessionRepositoryError
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
