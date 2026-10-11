from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolPermissionMetadata
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.models import DirectoryGrant, PermissionResolution, PermissionRule
from lancher_code.permissions.storage import PermissionRuleFileError, PermissionStorage
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.context import ToolContext


def _call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(0, "call-0", name, arguments, "{}")


def _context(project: Path, *, phase: str = "execute", access: str = "project") -> ToolContext:
    return ToolContext(cwd=project, project_root=project, timeout_seconds=2,
                       work_phase=phase, execution_access=access)


def _check(project: Path, name: str, arguments: dict[str, object], *, phase: str = "execute",
           access: str = "project", storage: PermissionStorage | None = None):
    registry = create_default_tool_registry()
    return PermissionEngine(storage).evaluate(call=_call(name, arguments), tool=registry.get(name).definition,
                                              context=_context(project, phase=phase, access=access))


def _quote(path: Path) -> str:
    return f'"{path.as_posix()}"'


def _read_command(path: Path) -> str:
    return f"Get-Content {_quote(path)}" if os.name == "nt" else f"cat {_quote(path)}"


def test_old_rule_files_are_rejected_and_preserved(tmp_path: Path) -> None:
    path = tmp_path / "permissions.yaml"
    original = 'rules:\n  - match: "RunCommand(git *)"\n    result: deny\n    match_kind: glob\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(PermissionRuleFileError, match="旧格式不再支持") as captured:
        PermissionStorage(project_rules_path=path)
    assert str(path) in str(captured.value)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("text", ["", "null", "[]", "format_version: 1\nrules: []", "format_version: true\nrules: []",
                                 "format_version: 2\nrules: null", "format_version: 2\nrules: {}"])
def test_invalid_rule_documents_are_not_silently_reset(tmp_path: Path, text: str) -> None:
    path = tmp_path / "permissions.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PermissionRuleFileError):
        PermissionStorage(project_rules_path=path)
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("result,kind", [("allow", "exact"), ("[]", "exact"), ("deny", "[]"), ("deny", "legacy")])
def test_invalid_new_rule_entries_report_path_and_preserve_file(tmp_path: Path, result: str, kind: str) -> None:
    path = tmp_path / "permissions.yaml"
    original = f"format_version: 2\nrules:\n  - match: RunCommand(git status)\n    result: {result}\n    match_kind: {kind}\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(PermissionRuleFileError) as captured:
        PermissionStorage(project_rules_path=path)
    assert str(path) in str(captured.value)
    assert path.read_text(encoding="utf-8") == original


def test_atomic_rule_write_failure_preserves_disk_memory_and_cleans_temporary(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "permissions.yaml"
    original = 'format_version: 2\nrules:\n  - match: "RunCommand(git *)"\n    result: deny\n    match_kind: glob\n'
    path.write_text(original, encoding="utf-8")
    storage = PermissionStorage(project_rules_path=path)
    monkeypatch.setattr("lancher_code.permissions.storage.os.replace", lambda *args: (_ for _ in ()).throw(OSError("失败")))
    with pytest.raises(PermissionRuleFileError, match="无法保存"):
        storage.add_project_rule("RunCommand(npm *)", "deny", match_kind="glob")
    assert path.read_text(encoding="utf-8") == original
    assert [rule.match for rule in storage.rules_for_scope("project")] == ["RunCommand(git *)"]
    assert list(tmp_path.iterdir()) == [path]


def test_rule_file_roundtrip_contains_only_versioned_denies(tmp_path: Path) -> None:
    path = tmp_path / "permissions.yaml"
    storage = PermissionStorage(project_rules_path=path)
    storage.add_project_rule("RunCommand(git *)", "deny", match_kind="glob", command_cwd=str(tmp_path),
                             command_shell="powershell" if os.name == "nt" else "sh")
    assert "format_version: 2" in path.read_text(encoding="utf-8")
    assert PermissionStorage(project_rules_path=path).rules_for_scope("project") == storage.rules_for_scope("project")


@pytest.mark.parametrize("scope", ["session", "project", "user"])
def test_old_allow_rules_are_rejected_in_memory(tmp_path: Path, scope: str) -> None:
    storage = PermissionStorage(project_rules_path=tmp_path / "project.yaml", user_rules_path=tmp_path / "user.yaml")
    rule = PermissionRule("ReadFile(*)", "allow", scope, "glob")
    with pytest.raises(ValueError, match="只支持 deny"):
        if scope == "session":
            storage.replace_session_rules([rule])
        else:
            storage.replace_rules(scope, [rule])
    assert storage.rules_for_scope(scope) == []


def test_session_grants_notify_deduplicate_and_never_write_rules_yaml(tmp_path: Path) -> None:
    path = tmp_path / "permissions.yaml"
    storage = PermissionStorage(project_rules_path=path)
    notifications = []
    storage.subscribe_session_rules_changed(lambda: notifications.append(True))
    grant = DirectoryGrant(str(tmp_path.resolve()), "read", True)
    storage.add_directory_grants([grant, grant])
    storage.add_directory_grants([grant])
    assert storage.session_grants == [grant]
    assert notifications == [True]
    storage.replace_session_grants([], notify=False)
    assert notifications == [True] and not path.exists()


@pytest.mark.parametrize("grant", [DirectoryGrant("relative"), DirectoryGrant(""), DirectoryGrant("/absolute", "bad"),
                                  DirectoryGrant("/absolute", "read", "yes")])
def test_invalid_directory_grants_do_not_replace_current_grants(tmp_path: Path, grant: DirectoryGrant) -> None:
    storage = PermissionStorage()
    initial = DirectoryGrant(str(tmp_path.resolve()))
    storage.add_directory_grants([initial])
    with pytest.raises(ValueError):
        storage.replace_session_grants([grant])
    assert storage.session_grants == [initial]


def test_nul_in_directory_grant_and_command_context_is_rejected_before_path_use(tmp_path: Path) -> None:
    from lancher_code.permissions.storage import validate_command_context
    storage = PermissionStorage()
    with pytest.raises(ValueError, match="绝对路径"):
        storage.add_directory_grants([DirectoryGrant(str(tmp_path) + "\x00bad")])
    with pytest.raises(ValueError, match="绝对路径"):
        validate_command_context(str(tmp_path) + "\x00bad", "powershell" if os.name == "nt" else "sh")
    assert storage.session_grants == []


@pytest.mark.parametrize("access", ["project", "full"])
@pytest.mark.parametrize("name,args", [("read_file", {"path": "data.txt"}), ("write_file", {"path": "new.txt", "content": "hello"}),
                                       ("edit_file", {"path": "data.txt", "old_text": "data", "new_text": "next"}),
                                       ("glob", {"pattern": "**/*.txt"}), ("grep", {"pattern": "data"}),
                                       ("delete_file", {"path": "data.txt"})])
def test_project_file_operations_execute_without_approval(tmp_path: Path, access: str, name: str, args: dict) -> None:
    (tmp_path / "data.txt").write_text("data", encoding="utf-8")
    assert _check(tmp_path, name, args, access=access).decision == "allow"


@pytest.mark.parametrize("phase", ["discuss", "plan"])
@pytest.mark.parametrize("access", ["project", "full"])
@pytest.mark.parametrize("name,args", [("write_file", {"path": "new.txt", "content": "hello"}),
                                       ("edit_file", {"path": "data.txt", "old_text": "data", "new_text": "next"}),
                                       ("delete_file", {"path": "data.txt"})])
def test_readonly_phase_overrides_full_access_for_every_normal_write(tmp_path: Path, phase: str, access: str, name: str, args: dict) -> None:
    (tmp_path / "data.txt").write_text("data", encoding="utf-8")
    check = _check(tmp_path, name, args, phase=phase, access=access)
    assert check.decision == "deny" and check.reason_code == "phase_disallowed"


@pytest.mark.parametrize("phase", ["discuss", "plan", "execute"])
@pytest.mark.parametrize("access", ["project", "full"])
@pytest.mark.parametrize("name", ["read_file", "glob", "grep"])
def test_external_read_needs_approval_except_full_execution(tmp_path: Path, phase: str, access: str, name: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    args = {"path": str(target if name == "read_file" else outside)}
    if name != "read_file":
        args["pattern"] = "data" if name == "grep" else "**/*.txt"
    check = _check(project, name, args, phase=phase, access=access)
    if phase == "execute" and access == "full":
        assert check.decision == "allow"
    else:
        assert check.decision == "ask" and check.request is not None
        assert check.request.directory_grants == [DirectoryGrant(str(outside.resolve()), "read", True)]
        assert str(target if name == "read_file" else outside) in check.request.details


@pytest.mark.parametrize("access,decision", [("project", "ask"), ("full", "allow")])
def test_external_write_needs_directory_scope_in_project_mode(tmp_path: Path, access: str, decision: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    check = _check(project, "write_file", {"path": str(outside / "new.txt"), "content": "hello"}, access=access)
    assert check.decision == decision
    if check.request is not None:
        assert check.request.directory_grants == [DirectoryGrant(str(outside), "write", True)]
        assert check.request.metadata["external_accesses"][0]["access"] == "write"


def test_read_grant_does_not_allow_writes_but_write_grant_allows_reads(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    storage = PermissionStorage()
    storage.add_directory_grants([DirectoryGrant(str(outside), "read")])
    assert _check(project, "read_file", {"path": str(target)}, storage=storage).decision == "allow"
    assert _check(project, "write_file", {"path": str(target), "content": "next"}, storage=storage).decision == "ask"
    storage.replace_session_grants([DirectoryGrant(str(outside), "write")])
    assert _check(project, "write_file", {"path": str(target), "content": "next"}, storage=storage).decision == "allow"
    assert _check(project, "read_file", {"path": str(target)}, storage=storage, phase="plan").decision == "allow"
    assert _check(project, "write_file", {"path": str(target), "content": "next"}, storage=storage, phase="plan").decision == "deny"


def test_nonrecursive_grant_only_covers_directory_and_direct_files(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); (outside / "nested").mkdir(parents=True)
    direct, nested = outside / "data.txt", outside / "nested" / "data.txt"
    direct.write_text("direct", encoding="utf-8"); nested.write_text("nested", encoding="utf-8")
    storage = PermissionStorage()
    storage.add_directory_grants([DirectoryGrant(str(outside), "read", False)])
    assert _check(project, "read_file", {"path": str(direct)}, storage=storage).decision == "allow"
    assert _check(project, "read_file", {"path": str(nested)}, storage=storage).decision == "ask"
    assert _check(project, "grep", {"path": str(outside), "pattern": "data"}, storage=storage).decision == "ask"


def test_approval_adds_only_session_directory_grants_and_does_not_change_execution_signature(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    storage = PermissionStorage(project_rules_path=project / ".lancher" / "permissions.yaml")
    engine = PermissionEngine(storage)
    call = _call("write_file", {"path": str(outside / "new.txt"), "content": "hello"})
    tool = create_default_tool_registry().get("write_file").definition
    context = _context(project)
    before = engine.evaluate(call=call, tool=tool, context=context)
    assert before.request is not None
    engine.apply_resolution(before.request, PermissionResolution(before.request.request_id, "allow_session"))
    after = engine.evaluate(call=call, tool=tool, context=context)
    assert after.decision == "allow"
    assert before.metadata["execution_signature"] == after.metadata["execution_signature"]
    assert storage.rules_for_scope("session") == storage.rules_for_scope("project") == []
    assert not storage.project_rules_path.exists()


def test_every_matching_deny_overrides_directory_grants_and_full_access(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    storage = PermissionStorage(user_rules_path=tmp_path / "user.yaml")
    target = outside / "secret.txt"
    storage.replace_rules("user", [PermissionRule(f"ReadFile({target.as_posix()})", "deny", "user")])
    storage.add_directory_grants([DirectoryGrant(str(outside), "write")])
    for access in ("project", "full"):
        check = _check(project, "read_file", {"path": str(target)}, storage=storage, access=access)
        assert check.decision == "deny" and check.reason_code == "permission_rule_deny"


@pytest.mark.parametrize("phase", ["discuss", "plan", "execute"])
@pytest.mark.parametrize("access", ["project", "full"])
@pytest.mark.parametrize("category", ["read", "command"])
def test_mcp_access_matrix_does_not_create_directory_authorization(tmp_path: Path, phase: str, access: str, category: str) -> None:
    tool = ToolDefinition("mcp__demo__tool", "demo", category=category, allowed_phases=("discuss", "plan", "execute"),
        permission=ToolPermissionMetadata("external", "mcp__demo__tool", "Demo", "demo", "tool"))
    check = PermissionEngine().evaluate(call=_call(tool.name, {"data": "example"}), tool=tool,
                                       context=_context(tmp_path, phase=phase, access=access))
    expected = "allow" if category == "read" else ("deny" if phase != "execute" else ("allow" if access == "full" else "ask"))
    assert check.decision == expected
    if check.request is not None:
        assert check.request.directory_grants == [] and check.request.metadata["allow_once_only"] is True
        with pytest.raises(ValueError, match="仅支持本次"):
            PermissionEngine().apply_resolution(check.request, PermissionResolution(check.request.request_id, "allow_session"))


@pytest.mark.parametrize("access", ["project", "full"])
def test_unknown_shell_and_project_cd_no_longer_need_review(tmp_path: Path, access: str) -> None:
    command = "$name = 'ready'; Write-Output $name" if os.name == "nt" else 'name=ready; printf "%s" "$name"'
    check = _check(tmp_path, "run_command", {"command": command}, access=access)
    assert check.decision == "allow" and check.metadata["analysis_unknown"]
    command = "Set-Location .; Write-Output ready" if os.name == "nt" else "cd .; printf ready"
    assert _check(tmp_path, "run_command", {"command": command}, access=access).decision == "allow"


@pytest.mark.parametrize("phase", ["discuss", "plan"])
def test_readonly_shell_queries_allowed_unknown_scripts_denied(tmp_path: Path, phase: str) -> None:
    target = tmp_path / "data.txt"
    target.write_text("data", encoding="utf-8")
    assert _check(tmp_path, "run_command", {"command": _read_command(target)}, phase=phase).decision == "allow"
    check = _check(tmp_path, "run_command", {"command": "python task.py"}, phase=phase, access="full")
    assert check.decision == "deny" and check.reason_code == "phase_disallowed"


@pytest.mark.parametrize("access,decision", [("project", "ask"), ("full", "allow")])
def test_static_shell_external_paths_approval_and_full_bypass(tmp_path: Path, access: str, decision: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    check = _check(project, "run_command", {"command": _read_command(target)}, access=access)
    assert check.decision == decision
    if check.request is not None:
        assert check.request.directory_grants == [DirectoryGrant(str(outside), "read")]
        assert check.request.command == _read_command(target)


@pytest.mark.parametrize("command", ["python task.py", "npm run build"])
def test_unknown_command_started_outside_project_requires_write_scope(tmp_path: Path, command: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    check = _check(project, "run_command", {"command": command, "cwd": str(outside)})
    assert check.decision == "ask" and check.request is not None
    assert check.request.metadata["cwd"] == str(outside)
    assert check.request.directory_grants == [DirectoryGrant(str(outside), "write")]


def test_external_readonly_command_started_outside_only_requests_read_scope(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    check = _check(project, "run_command", {"command": _read_command(target), "cwd": str(outside)}, phase="plan")
    assert check.decision == "ask" and check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(outside), "read")]


def test_deny_rules_apply_to_shell_subcommands(tmp_path: Path) -> None:
    storage = PermissionStorage()
    command = "Write-Output deny" if os.name == "nt" else "printf deny"
    storage.add_session_rule(f"RunCommand({command})", "deny")
    prefix = "Write-Output ready" if os.name == "nt" else "printf ready"
    check = _check(tmp_path, "run_command", {"command": prefix + "; " + command}, access="full", storage=storage)
    assert check.decision == "deny" and check.reason_code == "permission_rule_deny"


@pytest.mark.parametrize("name", ["process_write", "process_background"])
def test_process_control_in_external_startup_directory_requests_write_grant(tmp_path: Path, name: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    context = _context(project)
    context.session_id = "a" * 32
    context.execution_runtime = SimpleNamespace(processes=SimpleNamespace(get=lambda pid, sid: SimpleNamespace(cwd=str(outside))))
    call = _call(name, {"process_id": "b" * 32, "text": "hello"})
    check = PermissionEngine().evaluate(call=call, tool=create_default_tool_registry().get(name).definition, context=context)
    assert check.decision == "ask" and check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(outside), "write")]


@pytest.mark.parametrize("phase", ["discuss", "plan"])
def test_only_plan_file_special_tool_can_write_valid_session_plan(tmp_path: Path, phase: str) -> None:
    context = _context(tmp_path, phase=phase)
    context.session_id = "a" * 32
    context.session_root = tmp_path / ".lancher" / "sessions" / context.session_id
    context.session_workspace = context.session_root / "workspace"
    context.session_workspace.mkdir(parents=True)
    context.plan_file_path = context.session_workspace / "plan.md"
    registry = create_default_tool_registry()
    plan = PermissionEngine().evaluate(call=_call("write_plan_file", {"content": "计划"}),
                                       tool=registry.get("write_plan_file").definition, context=context)
    assert plan.decision == ("allow" if phase == "plan" else "deny")
    ordinary = PermissionEngine().evaluate(call=_call("write_file", {"path": str(context.plan_file_path), "content": "改写"}),
                                           tool=registry.get("write_file").definition, context=context)
    assert ordinary.decision == "deny"


@pytest.mark.parametrize("pattern", ["*.txt", "*/*.txt", "name?/data.txt", "[ab]/data.txt", "fixed*/nested/data.txt"])
@pytest.mark.parametrize("operation", ["read", "delete"])
def test_wildcard_requests_grant_real_directory_before_first_glob_component(tmp_path: Path, pattern: str, operation: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / pattern
    command = (_read_command(target) if operation == "read" else
               (f"Remove-Item {_quote(target)}" if os.name == "nt" else f"rm {_quote(target)}"))
    check = _check(project, "run_command", {"command": command})
    assert check.decision == "ask" and check.request is not None
    assert check.request.command == command
    assert check.request.directory_grants == [DirectoryGrant(str(outside.resolve()), "read" if operation == "read" else "write", True)]
    assert all(not any(character in part for character in "*?[") for grant in check.request.directory_grants for part in Path(grant.path).parts)
    assert check.metadata["execution_signature"]


@pytest.mark.parametrize("access", ["read", "write"])
def test_existing_ancestor_grant_covers_multilevel_wildcards_without_another_approval(tmp_path: Path, access: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "nested" / "*" / "data.txt"
    storage = PermissionStorage()
    storage.add_directory_grants([DirectoryGrant(str(outside.resolve()), access, True)])
    command = _read_command(target)
    assert _check(project, "run_command", {"command": command}, storage=storage).decision == "allow"
    deleting = f"Remove-Item {_quote(target)}" if os.name == "nt" else f"rm {_quote(target)}"
    assert _check(project, "run_command", {"command": deleting}, storage=storage).decision == ("allow" if access == "write" else "ask")


def test_session_approval_of_wildcards_keeps_exact_call_signature(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    storage = PermissionStorage()
    engine = PermissionEngine(storage)
    command = _read_command(outside / "*" / "*.txt")
    call = _call("run_command", {"command": command})
    tool = create_default_tool_registry().get("run_command").definition
    context = _context(project)
    before = engine.evaluate(call=call, tool=tool, context=context)
    assert before.request is not None and before.request.command == command
    engine.apply_resolution(before.request, PermissionResolution(before.request.request_id, "allow_session"))
    assert storage.session_grants == [DirectoryGrant(str(outside.resolve()), "read", True)]
    after = engine.evaluate(call=call, tool=tool, context=context)
    assert after.decision == "allow"
    assert before.metadata["execution_signature"] == after.metadata["execution_signature"]


@pytest.mark.parametrize("phase,access,location,decision", [
    ("execute", "project", "project", "allow"), ("execute", "full", "project", "allow"),
    ("execute", "project", "outside", "ask"), ("execute", "full", "outside", "allow"),
    ("discuss", "project", "outside", "deny"), ("plan", "full", "outside", "deny"),
])
def test_wildcard_scope_obeys_execution_and_readonly_modes(tmp_path: Path, phase: str, access: str, location: str, decision: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = (project if location == "project" else outside) / "*" / "*.txt"
    check = _check(project, "run_command", {"command": _read_command(target)}, phase=phase, access=access)
    assert check.decision == decision
    if decision == "deny":
        assert check.reason_code == "phase_disallowed"


def test_wildcard_grant_preserves_fixed_directory_prefix(tmp_path: Path) -> None:
    project, prefix = tmp_path / "project", tmp_path / "outside" / "fixed"
    project.mkdir(); prefix.mkdir(parents=True)
    check = _check(project, "run_command", {"command": _read_command(prefix / "*" / "*.txt")})
    assert check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(prefix.resolve()), "read", True)]


@pytest.mark.parametrize("name,args,access", [
    ("read_file", {}, "read"), ("write_file", {"content": "next"}, "write"), ("delete_file", {}, "write"),
])
def test_native_literal_bracket_directory_grant_stays_on_actual_directory(tmp_path: Path, name: str, args: dict, access: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside" / "[notes]"
    project.mkdir(); outside.mkdir(parents=True)
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    check = _check(project, name, {"path": str(target), **args})
    assert check.decision == "ask" and check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(outside.resolve()), access, True)]
    assert str(outside.parent) != check.request.directory_grants[0].path


def test_shell_startup_cwd_with_brackets_is_a_literal_directory_grant(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside" / "[notes]"
    project.mkdir(); outside.mkdir(parents=True)
    check = _check(project, "run_command", {"command": "python task.py", "cwd": str(outside)})
    assert check.decision == "ask" and check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(outside.resolve()), "write", True)]


@pytest.mark.parametrize("name", ["process_write", "process_background"])
def test_process_startup_cwd_with_brackets_is_literal(tmp_path: Path, name: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside" / "[notes]"
    project.mkdir(); outside.mkdir(parents=True)
    context = _context(project)
    context.session_id = "a" * 32
    context.execution_runtime = SimpleNamespace(processes=SimpleNamespace(get=lambda pid, sid: SimpleNamespace(cwd=str(outside))))
    check = PermissionEngine().evaluate(call=_call(name, {"process_id": "b" * 32, "text": "hello"}),
                                       tool=create_default_tool_registry().get(name).definition, context=context)
    assert check.decision == "ask" and check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(outside.resolve()), "write", True)]
