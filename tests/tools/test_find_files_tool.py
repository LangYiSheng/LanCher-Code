from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext
from lancher_code.tools.builtin.glob import FindFilesTool


def test_find_files_tool_returns_matching_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.py").write_text("", encoding="utf-8")
    tool = FindFilesTool()

    result = __import__("asyncio").run(
        tool.execute({"pattern": "**/*.py"}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is True
    assert len(result.payload["paths"]) == 2
    assert "a.py" in result.content
    assert "nested\\c.py" in result.content or "nested/c.py" in result.content


def test_find_files_tool_truncates_large_result_set_for_ui(tmp_path: Path) -> None:
    for index in range(205):
        (tmp_path / f"{index}.py").write_text("", encoding="utf-8")
    tool = FindFilesTool()

    result = __import__("asyncio").run(
        tool.execute({"pattern": "*.py"}, ToolContext(cwd=tmp_path, timeout_seconds=1))
    )

    assert result.ok is True
    assert result.payload["truncated"] is True
    assert len(result.payload["paths"]) == 200
    assert result.payload["total_matches"] == 205
