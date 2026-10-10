from __future__ import annotations

from lancher_code.tools.core.file_state_cache import FileStateCache

from pathlib import Path

from lancher_code.tools.context import ToolContext
from lancher_code.tools.builtin.grep import GrepTool


def test_search_code_tool_returns_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('hello')\nvalue = 1", encoding="utf-8")
    tool = GrepTool()

    result = __import__("asyncio").run(
        tool.execute({"pattern": "hello"}, ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1))
    )

    assert (not result.is_error) is True
    assert len(result.metadata["matches"]) == 1
    assert result.metadata["matches"][0]["line"] == 1
    assert "a.py:1:print('hello')" in result.content


def test_search_code_tool_respects_path_scope(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("hello", encoding="utf-8")
    tool = GrepTool()

    result = __import__("asyncio").run(
        tool.execute({"pattern": "hello", "path": "sub"}, ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1))
    )

    assert (not result.is_error) is True
    assert len(result.metadata["matches"]) == 1
    assert "sub" in result.metadata["matches"][0]["path"]


def test_search_code_tool_marks_large_result_for_ui_truncation(tmp_path: Path) -> None:
    lines = "\n".join("hello" for _ in range(250))
    (tmp_path / "a.py").write_text(lines, encoding="utf-8")
    tool = GrepTool()

    result = __import__("asyncio").run(
        tool.execute({"pattern": "hello"}, ToolContext(file_state_cache=FileStateCache(), cwd=tmp_path, timeout_seconds=1))
    )

    assert (not result.is_error) is True
    assert result.metadata["truncated"] is True
    assert len(result.metadata["matches"]) == 200
    assert result.metadata["total_matches"] == 250
