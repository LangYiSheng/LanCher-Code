from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext
from lancher_code.tools.builtin.read_file import ReadFileTool


def test_read_file_tool_reads_with_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("hello\nworld", encoding="utf-8")
    tool = ReadFileTool()

    result = __import__("asyncio").run(tool.execute({"path": "demo.txt"}, ToolContext(cwd=tmp_path, timeout_seconds=1)))

    assert result.ok is True
    assert f"文件: {path.resolve()}" in result.content
    assert "1\thello" in result.content
    assert "2\tworld" in result.content
    assert result.metadata["is_complete"] is True
    assert result.metadata["normalized_path"] == str(path.resolve())
    assert result.metadata["source_content"] == "hello\nworld"


def test_read_file_tool_returns_error_for_missing_file(tmp_path: Path) -> None:
    tool = ReadFileTool()

    result = __import__("asyncio").run(tool.execute({"path": "missing.txt"}, ToolContext(cwd=tmp_path, timeout_seconds=1)))

    assert result.ok is False
    assert result.error_code == "file_not_found"


def test_read_file_tool_requires_paging_for_large_file(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text("\n".join(f"line {index}" for index in range(500)), encoding="utf-8")
    tool = ReadFileTool()

    result = __import__("asyncio").run(tool.execute({"path": "large.txt"}, ToolContext(cwd=tmp_path, timeout_seconds=1)))

    assert result.ok is False
    assert result.error_code == "large_file_requires_paging"
    assert result.metadata["total_lines"] == 500


def test_read_file_tool_supports_offset_and_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text("\n".join(f"line {index}" for index in range(50)), encoding="utf-8")
    tool = ReadFileTool()

    result = __import__("asyncio").run(
        tool.execute({"path": str(path), "offset": 10, "limit": 3}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is True
    assert "11\tline 10" in result.content
    assert "13\tline 12" in result.content
    assert result.metadata["line_start"] == 11
    assert result.metadata["line_end"] == 13
    assert result.metadata["is_complete"] is False
    assert result.metadata["source_content"] == "line 10\nline 11\nline 12\n"
