from __future__ import annotations

from pathlib import Path

import pytest

from lancher_code.models import PermissionResolution, PermissionRule, ToolCall, ToolContext
from lancher_code.permission_engine import PermissionEngine, PermissionStorage
from lancher_code.tools.builtin.bash import BashTool
from lancher_code.tools.builtin.write_file import WriteFileTool


def _call(tool_name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        call_index=0,
        call_id="call-0",
        tool_name=tool_name,
        arguments=arguments,
        arguments_json="{}",
    )


def _context(tmp_path: Path, *, mode: str = "default") -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        project_root=tmp_path,
        timeout_seconds=1,
        mode=mode,  # type: ignore[arg-type]
    )


def test_blacklisted_command_is_denied_even_in_bypass_mode(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionStorage())

    check = engine.evaluate(
        call=_call("bash", {"description": "危险删除", "command": "Remove-Item -Recurse demo"}),
        tool=BashTool().definition,
        context=_context(tmp_path, mode="bypass"),
    )

    assert check.decision == "deny"
    assert check.reason_code == "permission_blacklist_denied"


def test_project_rule_overrides_user_rule(tmp_path: Path) -> None:
    user_rules = tmp_path / "home" / ".lancher" / "permissions.yaml"
    user_rules.parent.mkdir(parents=True, exist_ok=True)
    user_rules.write_text("rules:\n  - match: \"Bash(git *)\"\n    result: allow\n", encoding="utf-8")

    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True, exist_ok=True)
    project_rules.write_text("rules:\n  - match: \"Bash(git *)\"\n    result: deny\n", encoding="utf-8")

    engine = PermissionEngine(
        PermissionStorage(project_rules_path=project_rules, user_rules_path=user_rules)
    )

    check = engine.evaluate(
        call=_call("bash", {"description": "查看状态", "command": "git status"}),
        tool=BashTool().definition,
        context=_context(tmp_path),
    )

    assert check.decision == "deny"
    assert check.reason_code == "permission_rule_deny"


def test_session_rule_overrides_project_rule(tmp_path: Path) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True, exist_ok=True)
    project_rules.write_text("rules:\n  - match: \"Bash(git *)\"\n    result: deny\n", encoding="utf-8")

    storage = PermissionStorage(project_rules_path=project_rules)
    storage.add_session_rule("Bash(git *)", "allow")
    engine = PermissionEngine(storage)

    check = engine.evaluate(
        call=_call("bash", {"description": "查看状态", "command": "git status"}),
        tool=BashTool().definition,
        context=_context(tmp_path),
    )

    assert check.decision == "allow"


def test_replace_session_rules_normalizes_scope_and_notifies_without_touching_persistent_rules(
    tmp_path: Path,
) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True)
    project_rules.write_text(
        'rules:\n  - match: "Bash(git *)"\n    result: deny\n',
        encoding="utf-8",
    )
    storage = PermissionStorage(project_rules_path=project_rules)
    notifications: list[bool] = []
    storage.subscribe_session_rules_changed(lambda: notifications.append(True))

    storage.replace_session_rules(
        [PermissionRule(match="  Bash(pnpm *)  ", result="allow", scope="project")]
    )

    assert storage.rules_for_scope("session") == [
        PermissionRule(match="Bash(pnpm *)", result="allow", scope="session")
    ]
    assert storage.rules_for_scope("project") == [
        PermissionRule(match="Bash(git *)", result="deny", scope="project")
    ]
    assert notifications == [True]

    storage.replace_session_rules([], notify=False)
    assert notifications == [True]


def test_default_mode_asks_for_file_write(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionStorage())

    check = engine.evaluate(
        call=_call("write_file", {"path": "demo.txt", "content": "hello"}),
        tool=WriteFileTool().definition,
        context=_context(tmp_path, mode="default"),
    )

    assert check.decision == "ask"
    assert check.request is not None
    assert check.request.kind == "file_edit"


def test_accept_edits_mode_allows_file_write(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionStorage())

    check = engine.evaluate(
        call=_call("write_file", {"path": "demo.txt", "content": "hello"}),
        tool=WriteFileTool().definition,
        context=_context(tmp_path, mode="acceptEdits"),
    )

    assert check.decision == "allow"


def test_allow_project_resolution_persists_rule_to_project_file(tmp_path: Path) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    storage = PermissionStorage(project_rules_path=project_rules)
    engine = PermissionEngine(storage)

    check = engine.evaluate(
        call=_call("bash", {"description": "查看状态", "command": "git status"}),
        tool=BashTool().definition,
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
    assert "Bash(git *)" in project_rules.read_text(encoding="utf-8")


def test_rule_glob_matches_command_prefix(tmp_path: Path) -> None:
    project_rules = tmp_path / ".lancher" / "permissions.yaml"
    project_rules.parent.mkdir(parents=True, exist_ok=True)
    project_rules.write_text("rules:\n  - match: \"Bash(git *)\"\n    result: allow\n", encoding="utf-8")
    engine = PermissionEngine(PermissionStorage(project_rules_path=project_rules))

    check = engine.evaluate(
        call=_call("bash", {"description": "查看差异", "command": "git diff --stat"}),
        tool=BashTool().definition,
        context=_context(tmp_path),
    )

    assert check.decision == "allow"
