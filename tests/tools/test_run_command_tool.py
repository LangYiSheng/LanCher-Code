from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext
from lancher_code.tools.builtin.bash import RunCommandTool


def test_run_command_tool_captures_stdout_and_exit_code(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute({"command": 'Write-Output "hello"'}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is True
    assert result.payload["exit_code"] == 0
    assert "hello" in result.payload["stdout"]


def test_run_command_tool_returns_non_zero_result(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute({"command": 'Write-Error "boom"; exit 2'}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is False
    assert result.error_code == "non_zero_exit"
    assert result.payload["exit_code"] == 2


def test_run_command_tool_truncates_long_output(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(
        tool.execute({"command": 'Write-Output ("x" * 13000)'}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is True
    assert result.payload["truncated"] is True


def test_run_command_tool_rejects_invalid_arguments(tmp_path: Path) -> None:
    tool = RunCommandTool()

    result = __import__("asyncio").run(tool.execute({"command": ""}, ToolContext(cwd=tmp_path, timeout_seconds=1)))

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
