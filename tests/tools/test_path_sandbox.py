from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.models import ToolContext
from lancher_code.tools.builtin.glob import GlobTool
from lancher_code.tools.builtin.grep import GrepTool
from lancher_code.tools.builtin.read_file import ReadFileTool


def test_read_file_rejects_path_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    tool = ReadFileTool()

    result = asyncio.run(
        tool.execute({"path": str(outside)}, ToolContext(cwd=tmp_path, project_root=tmp_path, timeout_seconds=1))
    )

    assert result.ok is False
    assert result.error_code == "path_outside_project"


def test_glob_rejects_search_root_outside_project(tmp_path: Path) -> None:
    tool = GlobTool()

    result = asyncio.run(
        tool.execute(
            {"pattern": "*.txt", "path": str(tmp_path.parent)},
            ToolContext(cwd=tmp_path, project_root=tmp_path, timeout_seconds=1),
        )
    )

    assert result.ok is False
    assert result.error_code == "path_outside_project"


def test_grep_rejects_search_root_outside_project(tmp_path: Path) -> None:
    tool = GrepTool()

    result = asyncio.run(
        tool.execute(
            {"pattern": "hello", "path": str(tmp_path.parent)},
            ToolContext(cwd=tmp_path, project_root=tmp_path, timeout_seconds=1),
        )
    )

    assert result.ok is False
    assert result.error_code == "path_outside_project"


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="当前平台不支持符号链接测试")
def test_read_file_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-link-target.txt"
    outside.write_text("secret", encoding="utf-8")
    link_path = tmp_path / "link.txt"
    try:
        link_path.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境无法创建符号链接")

    tool = ReadFileTool()
    result = asyncio.run(
        tool.execute({"path": "link.txt"}, ToolContext(cwd=tmp_path, project_root=tmp_path, timeout_seconds=1))
    )

    assert result.ok is False
    assert result.error_code == "path_outside_project"
