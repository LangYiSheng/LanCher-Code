from pathlib import Path

import pytest

from lancher_code.models import ToolCall, ToolContext, ToolDefinition, ToolPermissionMetadata
from lancher_code.permission_engine import PermissionEngine, PermissionStorage


def external_definition(*, read_only: bool = False) -> ToolDefinition:
    name = "mcp__github__create_issue"
    return ToolDefinition(
        name=name,
        description="create",
        category="read" if read_only else "command",
        permission=ToolPermissionMetadata(
            source="external", rule_key=name, display_name="MCP github/create_issue",
            server_name="github", remote_tool_name="create_issue",
        ),
    )


@pytest.mark.parametrize(
    ("mode", "read_only", "decision"),
    [
        ("default", True, "allow"), ("default", False, "ask"),
        ("acceptEdits", False, "ask"), ("plan", True, "allow"),
        ("plan", False, "deny"), ("bypass", False, "allow"),
    ],
)
def test_external_tool_mode_matrix(tmp_path: Path, mode: str, read_only: bool, decision: str) -> None:
    engine = PermissionEngine()
    check = engine.evaluate(
        call=ToolCall(0, "call", "mcp__github__create_issue", {"title": "hello"}, "{}"),
        tool=external_definition(read_only=read_only),
        context=ToolContext(cwd=tmp_path, timeout_seconds=10, mode=mode),  # type: ignore[arg-type]
    )
    assert check.decision == decision
    if decision == "ask":
        assert check.request is not None and check.request.kind == "external_tool"
        assert check.request.session_rule == "mcp__github__create_issue"


def test_external_glob_rule_matches_visible_name(tmp_path: Path) -> None:
    rules = tmp_path / "permissions.yaml"
    rules.write_text('rules:\n  - match: "mcp__github__*"\n    result: allow\n', encoding="utf-8")
    engine = PermissionEngine(PermissionStorage(project_rules_path=rules))
    check = engine.evaluate(
        call=ToolCall(0, "call", "mcp__github__create_issue", {}, "{}"),
        tool=external_definition(),
        context=ToolContext(cwd=tmp_path, timeout_seconds=10),
    )
    assert check.decision == "allow"
