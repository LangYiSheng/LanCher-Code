from __future__ import annotations

from lancher_code.tools.core.file_state_cache import FileStateCache
from conftest import app_config_for

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import types as mcp_types

from lancher_code.config.loader import load_config_data
from lancher_code.config.writer import serialize_config
from lancher_code.errors import ConfigError
from lancher_code.mcp.adapter import MCPToolAdapter
from lancher_code.config.models import AppConfig, UIConfig
from lancher_code.sessions.models import PendingInput, PlanSnapshot
from lancher_code.permissions.models import PermissionResolution
from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolExecutionResult
from lancher_code.tools.context import ToolContext
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry


def _call(name: str, arguments: dict[str, object] | None = None, index: int = 0) -> ToolCall:
    return ToolCall(index, f"call-{index}", name, arguments or {}, "{}")


@pytest.mark.parametrize("phase", ["discuss", "plan"])
@pytest.mark.parametrize("policy", ["default", "acceptEdits", "bypass"])
def test_phase_boundaries_override_even_explicit_permission_rules(tmp_path, phase, policy):
    storage = PermissionStorage()
    storage.add_session_rule("RunCommand(*)", "allow", match_kind="glob")
    storage.add_session_rule("WriteFile(*)", "allow", match_kind="glob")
    storage.add_session_rule("mcp__demo__*", "allow", match_kind="glob")
    engine = PermissionEngine(storage)
    context = ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1, work_phase=phase, permission_policy=policy)
    remote = MCPToolAdapter("demo", mcp_types.Tool(name="write", inputSchema={}), None)
    for definition, call in [
        (RunCommandTool().definition, _call("run_command", {"command": "git status", "description": "状态"})),
        (WriteFileTool().definition, _call("write_file", {"path": "a.py", "content": "x"})),
        (remote.definition, _call(remote.definition.name)),
    ]:
        check = engine.evaluate(call=call, tool=definition, context=context)
        assert check.decision == "deny"
        assert check.reason_code == "phase_disallowed"


@pytest.mark.parametrize("phase", ["discuss", "plan"])
@pytest.mark.parametrize("annotation, expected", [(None, False), (False, False), (True, True)])
def test_mcp_discovery_and_permission_share_explicit_readonly_boundary(tmp_path, phase, annotation, expected):
    annotations = None if annotation is None else mcp_types.ToolAnnotations(readOnlyHint=annotation)
    tool = MCPToolAdapter("demo", mcp_types.Tool(name="lookup", inputSchema={}, annotations=annotations), None)
    registry = ToolRegistry()
    registry.register(tool, deferred_server_name="demo")
    registry.register_deferred_server("demo", title="Demo", description=None)
    matches = registry.search_deferred("lookup", work_phase=phase)
    assert bool(matches) is expected
    assert bool(registry.list_deferred_index(work_phase=phase)) is expected
    check = PermissionEngine().evaluate(
        call=_call(tool.definition.name), tool=tool.definition,
        context=ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1, work_phase=phase, permission_policy="bypass"),
    )
    assert (check.decision == "allow") is expected


def test_discuss_and_plan_share_tools_except_plan_writer(openai_provider_config, tmp_path):
    session = SessionController(openai_provider_config, cwd=tmp_path)
    registry = create_default_tool_registry()
    session.set_permission_policy("bypass")
    session.set_work_phase("discuss")
    request = session.build_request(registry.list_definitions(), allow_tool_calls=True)
    assert request.work_phase == "discuss"
    assert request.permission_policy == "bypass"
    assert {tool.name for tool in request.tools} == {"read_file", "write_file", "edit_file", "glob", "grep", "tool_search", "process_list", "process_read", "process_wait", "process_stop"}
    session.set_work_phase("plan")
    plan_tools = registry.list_definitions(work_phase=session.work_phase)
    assert {tool.name for tool in plan_tools} == {tool.name for tool in request.tools} | {"write_plan_file"}
    session.set_work_phase("execute")
    assert session.permission_policy == "bypass"


def test_exact_command_grant_keeps_wildcards_case_and_quoted_whitespace_literal(tmp_path):
    storage = PermissionStorage()
    engine = PermissionEngine(storage)
    definition = RunCommandTool().definition
    context = ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1)
    command = 'Write-Output "A *  B"'
    request = engine.evaluate(call=_call("run_command", {"command": command}), tool=definition, context=context).request
    assert request is not None and request.match_kind == "exact"
    engine.apply_resolution(request, PermissionResolution(request.request_id, "allow_session"))
    for candidate, expected in [
        (command, "allow"), ('Write-Output "A Z  B"', "ask"),
        ('Write-Output "A * B"', "ask"), ('Write-Output "a *  B"', "ask"),
        ('Write-Output "A *  B" -NoNewline', "ask"),
    ]:
        assert engine.evaluate(call=_call("run_command", {"command": candidate}), tool=definition, context=context).decision == expected


def test_exact_file_grant_does_not_treat_brackets_as_glob(tmp_path):
    storage = PermissionStorage()
    engine = PermissionEngine(storage)
    context = ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1)
    definition = WriteFileTool().definition
    request = engine.evaluate(call=_call("write_file", {"path": "[x].txt", "content": "x"}), tool=definition, context=context).request
    assert request is not None
    assert request.session_rule == "WriteFile([x].txt)"
    engine.apply_resolution(request, PermissionResolution(request.request_id, "allow_session"))
    assert engine.evaluate(call=_call("write_file", {"path": "[x].txt"}), tool=definition, context=context).decision == "allow"
    assert engine.evaluate(call=_call("write_file", {"path": "x.txt"}), tool=definition, context=context).decision == "ask"


def test_session_restores_stable_identity_plan_and_only_paused_pending_inputs(openai_provider_config, tmp_path):
    session = SessionController(openai_provider_config, cwd=tmp_path)
    session.set_work_phase("plan")
    session.set_permission_policy("acceptEdits")
    session.create_user_message("制定计划")
    plan = session.set_plan_snapshot("# 本会话计划", source_message_id="assistant-plan", ready=True)
    session.update_pending_inputs([
        PendingInput("one", "随后检查", "follow_up", "task-one"),
        PendingInput("two", "立即调整", "steer", "task-one"),
    ])
    saved_id = session.session_id
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)
    assert restored.session_id == session.session_id
    assert restored.plan_snapshot == plan
    assert restored.work_phase == "plan" and restored.permission_policy == "acceptEdits"
    assert [item.state for item in restored.pending_inputs] == ["paused", "paused"]
    restored.create_user_message("再修改计划")
    assert restored.plan_snapshot is not None and not restored.plan_snapshot.ready






def test_ui_config_and_plan_axes_roundtrip(openai_provider_config):
    data = serialize_config(app_config_for(provider=openai_provider_config))
    data["runtime"] = {"work_phase": "plan", "permission_policy": "default"}
    data["ui"] = {"theme": "dark", "busy_enter_action": "steer"}
    config = load_config_data(data)
    assert config.runtime.work_phase == "plan" and config.runtime.permission_policy == "default"
    saved = serialize_config(config)
    assert "permission_mode" not in saved["runtime"]
    assert saved["ui"]["busy_enter_action"] == "steer"
    assert UIConfig().busy_enter_action == "follow_up"
    data["ui"]["busy_enter_action"] = "unknown"
    with pytest.raises(ConfigError, match="busy_enter_action"):
        load_config_data(data)


def test_independent_axes_and_light_theme_roundtrip(openai_provider_config):
    data = serialize_config(app_config_for(provider=openai_provider_config))
    data["runtime"].update(work_phase="discuss", permission_policy="acceptEdits")
    data["ui"].update(theme="light", busy_enter_action="draft")
    config = load_config_data(data)
    assert config.runtime.work_phase == "discuss"
    assert config.runtime.permission_policy == "acceptEdits"
    assert config.ui.theme == "light"
    reloaded = load_config_data(serialize_config(config))
    assert reloaded.ui.theme == "light"
    assert reloaded.ui.busy_enter_action == "draft"


class _RecordingTool:
    def __init__(self, name, events, *, safe=True, wait=None, after=None, category="read"):
        self.definition = ToolDefinition(name=name, description=name, category=category)
        self.safe = safe
        self.events, self.wait, self.after = events, wait, after

    def resource_claims(self, arguments, context):
        from lancher_code.execution.contracts import ResourceClaim
        from lancher_code.execution.scheduler import project_claim
        return (ResourceClaim("external", "test:read", "shared"),) if self.safe else (project_claim(context.project_root),)

    async def execute(self, arguments, context):
        self.events.append(self.definition.name)
        if self.wait is not None:
            await self.wait.wait()
        if self.after is not None:
            self.after()
        return ToolExecutionResult("", self.definition.name, content="done", is_error=False)


@pytest.mark.asyncio
async def test_executor_finishes_started_parallel_group_then_skips_later_groups(tmp_path):
    registry, events, release = ToolRegistry(), [], asyncio.Event()
    interrupted = False

    def steer():
        nonlocal interrupted
        interrupted = True

    registry.register(_RecordingTool("one", events, wait=release, after=steer))
    registry.register(_RecordingTool("two", events, wait=release))
    registry.register(_RecordingTool("three", events, safe=False))
    registry.register(_RecordingTool("four", events))
    executor = ToolExecutor(registry, cwd=tmp_path)
    task = asyncio.create_task(executor.execute_calls(
        [_call(name, index=index) for index, name in enumerate(("one", "two", "three", "four"))],
        should_interrupt=lambda: interrupted,
    ))
    for _ in range(20):
        if len(events) == 2:
            break
        await asyncio.sleep(0)
    assert events == ["one", "two"]
    release.set()
    results = await task
    assert [result.error_code for result in results] == [None, None, "steering_superseded", "steering_superseded"]
    assert events == ["one", "two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["allow_session", "allow_project", "superseded"])
async def test_executor_interrupt_after_approval_does_not_execute_or_persist_grant(tmp_path, outcome):
    events, registry = [], ToolRegistry()
    registry.register(_RecordingTool("run_command", events, safe=False, category="command"))
    storage = PermissionStorage(project_rules_path=tmp_path / "permissions.yaml")
    executor = ToolExecutor(registry, cwd=tmp_path, permission_engine=PermissionEngine(storage))
    interrupted = False

    async def approve(request):
        nonlocal interrupted
        interrupted = outcome != "superseded"
        return PermissionResolution(request.request_id, outcome)

    results = await executor.execute_calls(
        [_call("run_command", {"command": "git status", "description": "查看状态"})],
        permission_resolver=approve, should_interrupt=lambda: interrupted,
    )
    assert results[0].error_code == "steering_superseded"
    assert events == []
    assert storage.rules_for_scope("session") == []
    assert storage.rules_for_scope("project") == []
    assert not (tmp_path / "permissions.yaml").exists()
