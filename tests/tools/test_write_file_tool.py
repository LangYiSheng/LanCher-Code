from __future__ import annotations

from lancher_code.tools.core.file_state_cache import FileStateCache

from pathlib import Path

from lancher_code.tools.context import ToolContext
from lancher_code.tools.builtin.read_file import ReadFileTool
from lancher_code.tools.builtin.write_file import WriteFileTool


def test_write_file_tool_creates_parent_directories(tmp_path: Path) -> None:
    tool = WriteFileTool()

    result = __import__("asyncio").run(
        tool.execute(
            {"path": "nested/demo.txt", "content": "hello"},
            ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1),
        )
    )

    assert (not result.is_error) is True
    assert (tmp_path / "nested" / "demo.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_tool_overwrites_existing_file_after_full_read(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("old", encoding="utf-8")
    read_tool = ReadFileTool()
    write_tool = WriteFileTool()
    context = ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1)

    read_result = __import__("asyncio").run(read_tool.execute({"path": "demo.txt"}, context))
    assert (not read_result.is_error) is True

    result = __import__("asyncio").run(write_tool.execute({"path": "demo.txt", "content": "new"}, context))

    assert (not result.is_error) is True
    assert path.read_text(encoding="utf-8") == "new"


def test_write_file_tool_rejects_overwrite_without_read(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("old", encoding="utf-8")
    tool = WriteFileTool()

    result = __import__("asyncio").run(
        tool.execute({"path": "demo.txt", "content": "new"}, ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1))
    )

    assert (not result.is_error) is False
    assert result.error_code == "stale_file_state"


def test_write_file_tool_rejects_invalid_content(tmp_path: Path) -> None:
    tool = WriteFileTool()

    result = __import__("asyncio").run(
        tool.execute({"path": "demo.txt", "content": 1}, ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1))
    )

    assert (not result.is_error) is False
    assert result.error_code == "invalid_arguments"
