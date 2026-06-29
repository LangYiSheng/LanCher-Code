from __future__ import annotations

from pathlib import Path

import pytest

from lancher_code.models import ToolContext
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.bash import BashTool
from lancher_code.tools.builtin.write_plan_file import WritePlanFileTool


def test_session_controller_filters_tools_for_plan_mode(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(
        openai_provider_config,
        cwd=tmp_path,
        plan_file_path=Path("./.lancher/plan.md"),
    )
    controller.set_runtime_mode("plan")
    registry = create_default_tool_registry()

    request = controller.build_request(
        registry.list_definitions(mode=controller.runtime_mode),
        allow_tool_calls=True,
    )

    tool_names = [tool.name for tool in request.tools]
    assert tool_names == ["read_file", "bash", "glob", "grep", "write_plan_file"]
    assert "write_file" not in tool_names
    assert "edit_file" not in tool_names


def test_bash_tool_allows_readonly_command_in_plan_mode(tmp_path: Path) -> None:
    tool = BashTool()

    result = __import__("asyncio").run(
        tool.execute({"command": "Get-ChildItem"}, ToolContext(cwd=tmp_path, timeout_seconds=1, mode="plan"))
    )

    assert result.ok is True


def test_bash_tool_rejects_side_effect_command_in_plan_mode(tmp_path: Path) -> None:
    tool = BashTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"command": 'Set-Content demo.txt "boom"'},
            ToolContext(cwd=tmp_path, timeout_seconds=1, mode="plan"),
        )
    )

    assert result.ok is False
    assert result.error_code == "plan_mode_command_rejected"


def test_write_plan_file_tool_only_writes_configured_path(tmp_path: Path) -> None:
    tool = WritePlanFileTool()
    plan_path = tmp_path / ".lancher" / "plan.md"

    result = __import__("asyncio").run(
        tool.execute(
            {"content": "# Plan\n\ncontent"},
            ToolContext(
                cwd=tmp_path,
                timeout_seconds=1,
                mode="plan",
                plan_file_path=plan_path,
            ),
        )
    )

    assert result.ok is True
    assert plan_path.read_text(encoding="utf-8") == "# Plan\n\ncontent"
