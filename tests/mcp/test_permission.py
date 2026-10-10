from pathlib import Path

import pytest

from lancher_code.contracts.tools import ToolCall, ToolDefinition, ToolPermissionMetadata
from lancher_code.tools.context import ToolContext
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.storage import PermissionStorage


def external_definition(*, read_only: bool = False) -> ToolDefinition:
    name = "mcp__github__create_issue"
    return ToolDefinition(
        name=name,
        description="create",
        category="read" if read_only else "command",
        allowed_phases=("discuss", "plan", "execute") if read_only else ("execute",),
        permission=ToolPermissionMetadata(
            source="external", rule_key=name, display_name="MCP github/create_issue",
            server_name="github", remote_tool_name="create_issue",
        ),
    )


@pytest.mark.parametrize(
    ("phase", "policy", "read_only", "decision"),
    [
        ("execute", "default", True, "allow"), ("execute", "default", False, "ask"),
        ("execute", "acceptEdits", False, "ask"), ("plan", "default", True, "allow"),
        ("plan", "default", False, "deny"), ("execute", "bypass", False, "allow"),
        ("discuss", "default", True, "allow"), ("discuss", "bypass", False, "deny"),
    ],
)
def test_external_tool_phase_and_policy_matrix(tmp_path: Path, phase: str, policy: str, read_only: bool, decision: str) -> None:
    engine = PermissionEngine()
    check = engine.evaluate(
        call=ToolCall(0, "call", "mcp__github__create_issue", {"title": "hello"}, "{}"),
        tool=external_definition(read_only=read_only),
        context=ToolContext(cwd=tmp_path, timeout_seconds=10, work_phase=phase, permission_policy=policy),  # type: ignore[arg-type]
    )
    assert check.decision == decision
    if decision == "ask":
        assert check.request is not None and check.request.kind == "external_tool"
        assert check.request.session_rule == "mcp__github__create_issue"


def test_external_glob_rule_matches_visible_name(tmp_path: Path) -> None:
    rules = tmp_path / "permissions.yaml"
    rules.write_text('rules:\n  - match: "mcp__github__*"\n    result: allow\n    match_kind: glob\n', encoding="utf-8")
    engine = PermissionEngine(PermissionStorage(project_rules_path=rules))
    check = engine.evaluate(
        call=ToolCall(0, "call", "mcp__github__create_issue", {}, "{}"),
        tool=external_definition(),
        context=ToolContext(cwd=tmp_path, timeout_seconds=10),
    )
    assert check.decision == "allow"
