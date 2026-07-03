from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext
from lancher_code.tools.builtin.bash import RunCommandTool


def test_run_command_tool_captures_stdout_and_exit_code(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"description": "输出问候语", "command": 'Write-Output "hello"'},
            ToolContext(cwd=tmp_path, timeout_seconds=1),
        )
    )

    assert result.ok is True
    assert result.payload["description"] == "输出问候语"
    assert result.payload["exit_code"] == 0
    assert "hello" in result.payload["stdout"]
    assert "描述: 输出问候语" in result.content


def test_run_command_tool_returns_non_zero_result(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"description": "制造失败退出", "command": 'Write-Error "boom"; exit 2'},
            ToolContext(cwd=tmp_path, timeout_seconds=1),
        )
    )

    assert result.ok is False
    assert result.error_code == "non_zero_exit"
    assert result.payload["description"] == "制造失败退出"
    assert result.payload["exit_code"] == 2


def test_run_command_tool_truncates_long_output(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"description": "输出超长文本", "command": 'Write-Output ("x" * 13000)'},
            ToolContext(cwd=tmp_path, timeout_seconds=1),
        )
    )

    assert result.ok is True
    assert result.payload["description"] == "输出超长文本"
    assert result.payload["truncated"] is True


def test_run_command_tool_rejects_empty_command(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute({"description": "命令为空", "command": ""}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"


def test_run_command_tool_rejects_missing_description(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute({"command": 'Write-Output "hello"'}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"


def test_run_command_tool_rejects_empty_description(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"description": "   ", "command": 'Write-Output "hello"'},
            ToolContext(cwd=tmp_path, timeout_seconds=1),
        )
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
