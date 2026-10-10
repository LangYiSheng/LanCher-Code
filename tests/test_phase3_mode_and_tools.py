from __future__ import annotations

from pathlib import Path

import pytest

from lancher_code.models import ToolContext
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.write_plan_file import WritePlanFileTool


def test_session_controller_filters_tools_for_plan_mode(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(
        openai_provider_config,
        cwd=tmp_path,
    )
    controller.set_runtime_mode("plan")
    registry = create_default_tool_registry()

    request = controller.build_request(
        registry.list_definitions(mode=controller.runtime_mode),
        allow_tool_calls=True,
    )

    tool_names = [tool.name for tool in request.tools]
    assert set(tool_names) == {"read_file", "write_file", "edit_file", "glob", "grep", "write_plan_file", "tool_search", "process_list", "process_read", "process_wait", "process_stop"}


def test_run_command_tool_rejects_even_readonly_command_in_plan_mode(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"description": "查看当前目录", "command": "Get-ChildItem"},
            ToolContext(cwd=tmp_path, timeout_seconds=1, mode="plan"),
        )
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert "execute" in result.error_message


def test_run_command_tool_rejects_side_effect_command_in_plan_mode(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"description": "尝试写入文件", "command": 'Set-Content demo.txt "boom"'},
            ToolContext(cwd=tmp_path, timeout_seconds=1, mode="plan"),
        )
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert "execute" in result.error_message


def test_write_plan_file_tool_only_writes_configured_path(tmp_path: Path) -> None:
    tool = WritePlanFileTool()
    session_id = "a" * 32
    session_root = tmp_path / ".lancher" / "sessions" / session_id
    workspace = session_root / "workspace"
    plan_path = workspace / "plan.md"

    result = __import__("asyncio").run(
        tool.execute(
            {"content": "# Plan\n\ncontent"},
            ToolContext(
                cwd=tmp_path,
                timeout_seconds=1,
                mode="plan",
                plan_file_path=plan_path,
                session_id=session_id,
                session_root=session_root,
                session_workspace=workspace,
            ),
        )
    )

    assert result.ok is True
    assert plan_path.read_text(encoding="utf-8") == "# Plan\n\ncontent"
