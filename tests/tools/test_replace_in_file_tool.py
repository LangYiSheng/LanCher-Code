from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext
from lancher_code.tools.builtin.edit_file import ReplaceInFileTool
from lancher_code.tools.builtin.read_file import ReadFileTool


def test_replace_in_file_tool_replaces_unique_match(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("hello world", encoding="utf-8")
    read_tool = ReadFileTool()
    tool = ReplaceInFileTool()
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)

    read_result = __import__("asyncio").run(read_tool.execute({"path": "demo.txt"}, context))
    assert read_result.ok is True

    result = __import__("asyncio").run(
        tool.execute(
            {"path": "demo.txt", "old_text": "world", "new_text": "LanCher"},
            context,
        )
    )

    assert result.ok is True
    assert path.read_text(encoding="utf-8") == "hello LanCher"
    assert result.metadata["line_start"] == 1
    assert result.metadata["display_lines"][0]["text"].startswith("- 1\t")
    assert result.metadata["display_lines"][1]["text"].startswith("+ 1\t")


def test_replace_in_file_tool_returns_error_for_zero_matches(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("hello world", encoding="utf-8")
    read_tool = ReadFileTool()
    tool = ReplaceInFileTool()
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    __import__("asyncio").run(read_tool.execute({"path": "demo.txt"}, context))

    result = __import__("asyncio").run(
        tool.execute(
            {"path": "demo.txt", "old_text": "missing", "new_text": "LanCher"},
            context,
        )
    )

    assert result.ok is False
    assert result.error_code == "match_not_found"
    assert result.metadata["match_count"] == 0


def test_replace_in_file_tool_returns_error_for_multiple_matches(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("hello\nhello", encoding="utf-8")
    read_tool = ReadFileTool()
    tool = ReplaceInFileTool()
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    __import__("asyncio").run(read_tool.execute({"path": "demo.txt"}, context))

    result = __import__("asyncio").run(
        tool.execute(
            {"path": "demo.txt", "old_text": "hello", "new_text": "LanCher"},
            context,
        )
    )

    assert result.ok is False
    assert result.error_code == "match_not_unique"
    assert result.metadata["match_count"] == 2
