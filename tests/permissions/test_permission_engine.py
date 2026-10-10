from __future__ import annotations

from lancher_code.tools.core.file_state_cache import FileStateCache

from pathlib import Path

import pytest

from lancher_code.permissions.models import PermissionResolution, PermissionRule
from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolPermissionMetadata
from lancher_code.tools.context import ToolContext
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.permissions.storage import PermissionRuleFileError
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.builtin.write_plan_file import WritePlanFileTool


def _call(tool_name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        call_index=0,
        call_id="call-0",
        tool_name=tool_name,
        arguments=arguments,
        arguments_json="{}",
    )


def test_project_rule_write_failure_keeps_disk_and_memory(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "permissions.yaml"
    original = "rules:\n  - match: RunCommand(git status)\n    result: allow\n    match_kind: exact\n"
    path.write_text(original, encoding="utf-8")
    storage = PermissionStorage(project_rules_path=path)

    def fail_replace(*args):
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr("lancher_code.permissions.storage.os.replace", fail_replace)
    with pytest.raises(PermissionRuleFileError, match="无法保存权限规则"):
        storage.add_project_rule("RunCommand(git diff)", "allow")
    assert path.read_text(encoding="utf-8") == original
    assert [rule.match for rule in storage.rules_for_scope("project")] == ["RunCommand(git status)"]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("invalid_field", ["result", "match_kind"])
def test_malformed_rule_reports_path_and_keeps_original(tmp_path: Path, invalid_field: str) -> None:
    path = tmp_path / "permissions.yaml"
    values = {"result": "allow", "match_kind": "exact", invalid_field: "[]"}
    original = f"rules:\n  - match: RunCommand(git status)\n    result: {values['result']}\n    match_kind: {values['match_kind']}\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(PermissionRuleFileError, match="权限规则") as captured:
        PermissionStorage(project_rules_path=path)
    assert str(path) in str(captured.value)
    assert path.read_text(encoding="utf-8") == original


def _context(tmp_path: Path, *, permission_policy: str = "default") -> ToolContext:
    return ToolContext(file_state_cache=FileStateCache(),
        cwd=tmp_path,
        project_root=tmp_path,
        timeout_seconds=1,
        permission_policy=permission_policy,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("kind", [None, "legacy"])
def test_old_rules_are_rejected_without_rewriting(tmp_path: Path, kind) -> None:
    path = tmp_path / "permissions.yaml"
    text = 'rules:\n  - match: "RunCommand(git *)"\n    result: allow\n'
    if kind is not None:
        text += f"    match_kind: {kind}\n"
    path.write_text(text, encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(PermissionRuleFileError, match="exact 或 glob") as error:
        PermissionStorage(project_rules_path=path)
    assert str(path) in str(error.value)
    assert path.read_bytes() == original


def test_builtin_phase_matrix_preserves_workspace_and_process_control() -> None:
    from lancher_code.tools import create_default_tool_registry
    registry = create_default_tool_registry()
    shared = {"read_file", "write_file", "edit_file", "glob", "grep", "tool_search",
              "process_list", "process_read", "process_wait", "process_stop"}
    for phase in ("discuss", "plan", "execute"):
        expected = shared | ({"write_plan_file"} if phase == "plan" else set())
        if phase == "execute":
            expected |= {"run_command", "process_write", "process_background"}
        assert {tool.name for tool in registry.list_definitions(work_phase=phase)} == expected


def test_blacklisted_command_is_denied_even_in_bypass_mode(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionStorage())

    check = engine.evaluate(
        call=_call("run_command", {"description": "危险删除", "command": "Remove-Item -Recurse demo"}),
        tool=RunCommandTool().definition,
        context=_context(tmp_path, permission_policy="bypass"),
    )

    assert check.decision == "deny"
    assert check.reason_code == "permission_blacklist_denied"


def test_project_rule_overrides_user_rule(tmp_path: Path) -> None:
    user_rules = tmp_path / "home" / ".lancher" / "permissions.yaml"
    user_rules.parent.mkdir(parents=True, exist_ok=True)
    user_rules.write_text("rules:\n  - match: \"RunCommand(git *)\"\n    result: allow\n    match_kind: glob\n", encoding="utf-8")

    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True, exist_ok=True)
    project_rules.write_text("rules:\n  - match: \"RunCommand(git *)\"\n    result: deny\n    match_kind: glob\n", encoding="utf-8")

    engine = PermissionEngine(
        PermissionStorage(project_rules_path=project_rules, user_rules_path=user_rules)
    )

    check = engine.evaluate(
        call=_call("run_command", {"description": "查看状态", "command": "git status"}),
        tool=RunCommandTool().definition,
        context=_context(tmp_path),
    )

    assert check.decision == "deny"
    assert check.reason_code == "permission_rule_deny"


def test_session_rule_overrides_project_rule(tmp_path: Path) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True, exist_ok=True)
    project_rules.write_text("rules:\n  - match: \"RunCommand(git *)\"\n    result: deny\n    match_kind: glob\n", encoding="utf-8")

    storage = PermissionStorage(project_rules_path=project_rules)
    storage.add_session_rule("RunCommand(git *)", "allow", match_kind="glob")
    engine = PermissionEngine(storage)

    check = engine.evaluate(
        call=_call("run_command", {"description": "查看状态", "command": "git status"}),
        tool=RunCommandTool().definition,
        context=_context(tmp_path),
    )

    assert check.decision == "allow"


def test_replace_session_rules_normalizes_scope_and_notifies_without_touching_persistent_rules(
    tmp_path: Path,
) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True)
    project_rules.write_text(
        'rules:\n  - match: "RunCommand(git *)"\n    result: deny\n    match_kind: glob\n',
        encoding="utf-8",
    )
    storage = PermissionStorage(project_rules_path=project_rules)
    notifications: list[bool] = []
    storage.subscribe_session_rules_changed(lambda: notifications.append(True))

    storage.replace_session_rules(
        [PermissionRule(match="  RunCommand(pnpm *)  ", result="allow", scope="project", match_kind="glob")]
    )

    assert storage.rules_for_scope("session") == [
        PermissionRule(match="RunCommand(pnpm *)", result="allow", scope="session", match_kind="glob")
    ]
    assert storage.rules_for_scope("project") == [
        PermissionRule(match="RunCommand(git *)", result="deny", scope="project", match_kind="glob")
    ]
    assert notifications == [True]

    storage.replace_session_rules([], notify=False)
    assert notifications == [True]


def test_default_mode_asks_for_file_write(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionStorage())

    check = engine.evaluate(
        call=_call("write_file", {"path": "demo.txt", "content": "hello"}),
        tool=WriteFileTool().definition,
        context=_context(tmp_path, permission_policy="default"),
    )

    assert check.decision == "ask"
    assert check.request is not None
    assert check.request.kind == "file_edit"


@pytest.mark.parametrize(
    ("tool_name", "phase", "policy", "arguments", "kind"),
    [
        ("run_command", "execute", "acceptEdits", {"command": "git status"}, "command"),
        ("write_file", "execute", "default", {"path": "demo.txt", "content": "hello"}, "file_edit"),
        ("mcp__github__create_issue", "execute", "acceptEdits", {"title": "问题"}, "external_tool"),
    ],
)
def test_permission_request_metadata_uses_tool_context_not_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name, phase, policy, arguments, kind,
) -> None:
    process_cwd = tmp_path / "launcher"
    tool_cwd = tmp_path / "project"
    process_cwd.mkdir()
    tool_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    external_name = "mcp__github__create_issue"
    definitions = {
        "run_command": RunCommandTool().definition,
        "write_file": WriteFileTool().definition,
        "write_plan_file": WritePlanFileTool().definition,
        external_name: ToolDefinition(
            name=external_name, description="创建问题", category="command",
            permission=ToolPermissionMetadata(
                source="external", rule_key=external_name, display_name="GitHub 创建问题",
                server_name="github", remote_tool_name="create_issue",
            ),
        ),
    }
    context = ToolContext(file_state_cache=FileStateCache(),
        cwd=tool_cwd, project_root=tool_cwd, timeout_seconds=1,
        work_phase=phase, permission_policy=policy,
        plan_file_path=tool_cwd / ".lancher" / "plan.md",
    )

    check = PermissionEngine().evaluate(
        call=_call(tool_name, arguments), tool=definitions[tool_name], context=context,
    )

    assert check.decision == "ask" and check.request is not None
    assert check.request.kind == kind
    assert check.request.metadata["cwd"] == str(tool_cwd)
    assert check.request.metadata["cwd"] != str(Path.cwd())
    assert check.request.metadata["work_phase"] == check.request.work_phase == phase
    assert check.request.metadata["permission_policy"] == check.request.permission_policy == policy
    assert check.request.metadata["work_phase"] == context.work_phase
    assert check.request.metadata["permission_policy"] == context.permission_policy
    if kind == "external_tool":
        assert check.request.metadata["server"] == "github"
        assert check.request.metadata["remote_tool"] == "create_issue"


def test_accept_edits_mode_allows_file_write(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionStorage())

    check = engine.evaluate(
        call=_call("write_file", {"path": "demo.txt", "content": "hello"}),
        tool=WriteFileTool().definition,
        context=_context(tmp_path, permission_policy="acceptEdits"),
    )

    assert check.decision == "allow"


def test_allow_project_resolution_persists_rule_to_project_file(tmp_path: Path) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    storage = PermissionStorage(project_rules_path=project_rules)
    engine = PermissionEngine(storage)

    check = engine.evaluate(
        call=_call("run_command", {"description": "查看状态", "command": "git status"}),
        tool=RunCommandTool().definition,
        context=_context(tmp_path),
    )
    assert check.request is not None

    engine.apply_resolution(
        check.request,
        PermissionResolution(
            request_id=check.request.request_id,
            outcome="allow_project",
        ),
    )

    assert project_rules.exists()
    assert "RunCommand(git status)" in project_rules.read_text(encoding="utf-8")
    assert "match_kind: exact" in project_rules.read_text(encoding="utf-8")


def test_rule_glob_matches_command_prefix(tmp_path: Path) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True, exist_ok=True)
    project_rules.write_text("rules:\n  - match: \"RunCommand(git *)\"\n    result: allow\n    match_kind: glob\n", encoding="utf-8")
    engine = PermissionEngine(PermissionStorage(project_rules_path=project_rules))

    check = engine.evaluate(
        call=_call("run_command", {"description": "查看差异", "command": "git diff --stat"}),
        tool=RunCommandTool().definition,
        context=_context(tmp_path),
    )

    assert check.decision == "allow"


@pytest.mark.parametrize("name", ["process_write", "process_background"])
@pytest.mark.parametrize("permission_policy", ["default", "acceptEdits"])
def test_process_input_and_transfer_need_once_only_approval(tmp_path: Path, name: str, permission_policy: str) -> None:
    from lancher_code.tools.builtin.process import ProcessTool

    storage = PermissionStorage()
    storage.add_session_rule(name, "allow")
    check = PermissionEngine(storage).evaluate(
        call=_call(name, {"process_id": "a" * 32, "text": "Write-Output hello\n"}),
        tool=ProcessTool(name).definition, context=_context(tmp_path, permission_policy=permission_policy),
    )
    assert check.decision == "ask" and check.request is not None
    assert check.request.session_rule is None and check.request.project_rule is None
    assert check.request.metadata["allow_once_only"] is True
    assert "Write-Output" in check.request.details


@pytest.mark.parametrize("name", ["process_write", "process_background"])
def test_explicit_process_deny_cannot_be_bypassed(tmp_path: Path, name: str) -> None:
    from lancher_code.tools.builtin.process import ProcessTool

    storage = PermissionStorage()
    storage.add_session_rule(name, "deny")
    check = PermissionEngine(storage).evaluate(
        call=_call(name, {"process_id": "a" * 32, "text": "echo hello\n"}),
        tool=ProcessTool(name).definition, context=_context(tmp_path, permission_policy="bypass"),
    )
    assert check.decision == "deny"


@pytest.mark.parametrize("phase", ["discuss", "plan", "execute"])
@pytest.mark.parametrize("name", ["process_list", "process_read", "process_wait", "process_stop"])
def test_observing_and_stopping_owned_processes_available_in_all_phases(tmp_path: Path, name: str, phase: str) -> None:
    from lancher_code.tools.builtin.process import ProcessTool

    check = PermissionEngine().evaluate(
        call=_call(name, {"process_id": "a" * 32}), tool=ProcessTool(name).definition,
        context=ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1, work_phase=phase),
    )
    assert check.decision == "allow"


@pytest.mark.parametrize("phase", ["discuss", "plan"])
@pytest.mark.parametrize("name", ["process_write", "process_background"])
def test_process_side_effects_require_execution_phase(tmp_path: Path, name: str, phase: str) -> None:
    from lancher_code.tools.builtin.process import ProcessTool

    check = PermissionEngine().evaluate(
        call=_call(name, {"process_id": "a" * 32, "text": "hello\n"}), tool=ProcessTool(name).definition,
        context=ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1, work_phase=phase, permission_policy="bypass"),
    )
    assert check.decision == "deny" and check.reason_code == "phase_disallowed"


def test_process_input_cannot_bypass_command_blacklist(tmp_path: Path) -> None:
    from lancher_code.tools.builtin.process import ProcessTool

    check = PermissionEngine().evaluate(
        call=_call("process_write", {"process_id": "a" * 32, "text": "echo ready\nRemove-Item target\n"}),
        tool=ProcessTool("process_write").definition, context=_context(tmp_path, permission_policy="bypass"),
    )
    assert check.decision == "deny" and check.reason_code == "permission_blacklist_denied"
