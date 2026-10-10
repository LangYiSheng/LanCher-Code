from __future__ import annotations

from lancher_code.tools.core.file_state_cache import FileStateCache

import os
import subprocess
from pathlib import Path

import pytest

from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolPermissionMetadata
from lancher_code.tools.context import ToolContext
from lancher_code.permissions.engine import PermissionEngine, PermissionStorage
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.edit_file import EditFileTool
from lancher_code.tools.builtin.glob import GlobTool
from lancher_code.tools.builtin.grep import GrepTool
from lancher_code.tools.builtin.read_file import ReadFileTool
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.builtin.write_plan_file import WritePlanFileTool
from lancher_code.tools.core.executor import ToolExecutor


def _context(project: Path, *, phase: str = "execute", policy: str = "default", identity: str = "a" * 32) -> ToolContext:
    root = project / ".lancher" / "sessions" / identity
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolContext(file_state_cache=FileStateCache(),
        cwd=project, project_root=project, timeout_seconds=1,
        work_phase=phase, permission_policy=policy,
        session_id=identity, session_root=root, session_workspace=workspace,
        plan_file_path=workspace / "plan.md",
    )


def _call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(0, "call", name, arguments, "{}")


@pytest.mark.parametrize("phase", ["discuss", "plan", "execute"])
@pytest.mark.parametrize("tool", [WriteFileTool(), EditFileTool()])
def test_current_workspace_writes_need_no_permission_prompt(tmp_path, phase, tool):
    context = _context(tmp_path, phase=phase)
    check = PermissionEngine().evaluate(
        call=_call(tool.definition.name, {"path": str(context.session_workspace / "note.md")}),
        tool=tool.definition, context=context,
    )
    assert check.decision == "allow"
    assert check.reason_code == "session_workspace_allowed"
    assert check.request is None


def test_plan_writer_is_bound_to_the_current_workspace(tmp_path):
    context = _context(tmp_path, phase="plan")
    definition = WritePlanFileTool().definition
    assert PermissionEngine().evaluate(call=_call("write_plan_file", {"content": "计划"}), tool=definition, context=context).decision == "allow"
    context.plan_file_path = tmp_path / "shared-plan.md"
    check = PermissionEngine().evaluate(call=_call("write_plan_file", {"content": "计划"}), tool=definition, context=context)
    assert check.decision == "deny"


def test_explicit_denial_beats_workspace_grant_and_higher_scope_allow(tmp_path):
    context = _context(tmp_path)
    storage = PermissionStorage(project_rules_path=tmp_path / ".lancher" / "permissions.yaml")
    storage.add_project_rule("WriteFile(*)", "deny", match_kind="glob")
    storage.add_session_rule("WriteFile(*)", "allow", match_kind="glob")
    check = PermissionEngine(storage).evaluate(
        call=_call("write_file", {"path": str(context.session_workspace / "note.md")}),
        tool=WriteFileTool().definition, context=context,
    )
    assert check.decision == "deny"
    assert check.reason_code == "permission_rule_deny"


@pytest.mark.parametrize("phase", ["discuss", "plan"])
@pytest.mark.parametrize("policy", ["default", "acceptEdits", "bypass"])
def test_project_and_other_workspace_writes_stay_readonly_during_investigation(tmp_path, phase, policy):
    context = _context(tmp_path, phase=phase, policy=policy)
    other = _context(tmp_path, identity="b" * 32)
    storage = PermissionStorage()
    storage.add_session_rule("WriteFile(*)", "allow", match_kind="glob")
    for path in (tmp_path / "code.py", other.session_workspace / "note.md"):
        check = PermissionEngine(storage).evaluate(call=_call("write_file", {"path": str(path)}), tool=WriteFileTool().definition, context=context)
        assert check.decision == "deny"
        assert check.reason_code == "phase_disallowed"


@pytest.mark.parametrize("policy", ["default", "acceptEdits", "bypass"])
@pytest.mark.parametrize("relative", ["session.jsonl", "state.json", "workspace/../permissions.json"])
def test_session_records_are_protected_from_structured_writes(tmp_path, policy, relative):
    context = _context(tmp_path, policy=policy)
    check = PermissionEngine().evaluate(
        call=_call("write_file", {"path": str(context.session_root / relative), "content": "tamper"}),
        tool=WriteFileTool().definition, context=context,
    )
    assert check.decision == "deny"
    assert check.reason_code == "session_records_protected"


def test_other_session_workspace_does_not_get_automatic_approval(tmp_path):
    context = _context(tmp_path)
    other = _context(tmp_path, identity="b" * 32)
    check = PermissionEngine().evaluate(
        call=_call("write_file", {"path": str(other.session_workspace / "note.md")}),
        tool=WriteFileTool().definition, context=context,
    )
    assert check.decision == "ask"


def test_control_directory_cannot_impersonate_a_session_workspace(tmp_path):
    context = _context(tmp_path, policy="bypass")
    check = PermissionEngine().evaluate(
        call=_call("write_file", {"path": str(tmp_path / ".lancher" / "sessions" / ".locks" / "workspace" / "lock.txt")}),
        tool=WriteFileTool().definition, context=context,
    )
    assert check.decision == "deny"
    assert check.reason_code == "session_records_protected"


@pytest.mark.skipif(os.name != "nt", reason="Windows 文件路径忽略大小写")
def test_windows_workspace_path_keeps_case_insensitive_authorization(tmp_path):
    context = _context(tmp_path)
    path = context.session_root / "WORKSPACE" / "note.md"
    check = PermissionEngine().evaluate(call=_call("write_file", {"path": str(path)}), tool=WriteFileTool().definition, context=context)
    assert check.decision == "allow"


def test_shell_and_external_tool_do_not_inherit_workspace_approval(tmp_path):
    context = _context(tmp_path)
    context.cwd = context.session_workspace
    external = ToolDefinition(
        name="mcp__demo__write", description="远程写入", category="command",
        permission=ToolPermissionMetadata(source="external", rule_key="mcp__demo__write", display_name="MCP 写入"),
    )
    for tool, arguments in (
        (RunCommandTool().definition, {"description": "打印", "command": "Write-Output hello"}),
        (external, {"path": str(context.session_workspace / "remote.txt")}),
    ):
        assert PermissionEngine().evaluate(call=_call(tool.name, arguments), tool=tool, context=context).decision == "ask"


@pytest.mark.asyncio
async def test_direct_file_tool_still_rejects_record_tampering(tmp_path):
    context = _context(tmp_path, policy="bypass")
    result = await WriteFileTool().execute({"path": str(context.session_root / "session.jsonl"), "content": "tamper"}, context)
    assert result.error_code == "session_records_protected"
    assert not (context.session_root / "session.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [WriteFileTool(), EditFileTool()])
async def test_workspace_hardlink_cannot_modify_control_records(tmp_path, tool):
    context = _context(tmp_path, phase="plan", policy="bypass")
    control = context.session_root / "events.jsonl"
    control.write_text("original", encoding="utf-8")
    link = context.session_workspace / "note.md"
    os.link(control, link)
    assert link.stat().st_nlink > 1
    await ReadFileTool().execute({"path": str(link)}, context)
    arguments = {"path": str(link), "content": "tampered", "old_text": "original", "new_text": "tampered"}

    check = PermissionEngine().evaluate(call=_call(tool.definition.name, arguments), tool=tool.definition, context=context)
    assert check.decision == "deny"
    assert check.reason_code == "hardlinked_file_protected"
    result = await tool.execute(arguments, context)
    assert result.error_code == "hardlinked_file_protected"
    assert control.read_text(encoding="utf-8") == link.read_text(encoding="utf-8") == "original"


def test_project_hardlink_is_protected_even_under_bypass_policy(tmp_path):
    context = _context(tmp_path, policy="bypass")
    file = tmp_path / "source.py"
    file.write_text("original", encoding="utf-8")
    link = tmp_path / "alias.py"
    os.link(file, link)
    check = PermissionEngine().evaluate(call=_call("write_file", {"path": str(link), "content": "tampered"}), tool=WriteFileTool().definition, context=context)
    assert check.decision == "deny"
    assert check.reason_code == "hardlinked_file_protected"


@pytest.mark.asyncio
async def test_file_read_state_is_reset_when_switching_sessions(tmp_path):
    first = _context(tmp_path)
    second = _context(tmp_path, identity="b" * 32)
    file = tmp_path / "code.py"
    file.write_text("original", encoding="utf-8")
    executor = ToolExecutor(create_default_tool_registry(), cwd=tmp_path)

    def parameters(context):
        return {"session_id": context.session_id, "session_root": context.session_root, "session_workspace": context.session_workspace, "permission_policy": "acceptEdits"}

    await executor.execute_calls([_call("read_file", {"path": "code.py"})], **parameters(first))
    results = await executor.execute_calls([_call("write_file", {"path": "code.py", "content": "new"})], **parameters(second))
    assert results[0].error_code == "stale_file_state"
    assert file.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_search_skips_session_data_by_default_and_supports_explicit_workspace(tmp_path):
    context = _context(tmp_path)
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    (context.session_workspace / "note.md").write_text("needle", encoding="utf-8")
    for tool, arguments in ((GlobTool(), {"pattern": "**/*"}), (GrepTool(), {"pattern": "needle"})):
        default = await tool.execute(arguments, context)
        assert "note.md" not in default.content
        explicit = await tool.execute({**arguments, "path": str(context.session_workspace)}, context)
        assert "note.md" in explicit.content


def _directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("系统不允许创建目录链接")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if result.returncode:
            pytest.skip("系统不允许创建目录链接或 junction")


def test_workspace_link_cannot_grant_project_file_write(tmp_path):
    context = _context(tmp_path, phase="plan")
    project_files = tmp_path / "src"
    project_files.mkdir()
    link = context.session_workspace / "code"
    _directory_link(link, project_files)
    check = PermissionEngine().evaluate(
        call=_call("write_file", {"path": str(link / "main.py")}), tool=WriteFileTool().definition, context=context,
    )
    assert check.decision == "deny"
    assert check.reason_code == "phase_disallowed"


def test_replaced_workspace_root_never_becomes_authorized_root(tmp_path):
    context = _context(tmp_path, phase="plan")
    context.session_workspace.rmdir()
    _directory_link(context.session_workspace, tmp_path)
    check = PermissionEngine().evaluate(
        call=_call("write_file", {"path": str(tmp_path / "main.py")}), tool=WriteFileTool().definition, context=context,
    )
    assert check.decision == "deny"
