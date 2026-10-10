from __future__ import annotations

import os
import stat
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from lancher_code.filesystem.access import PathSandboxError, PathWriteDeniedError, ensure_writable_path

if TYPE_CHECKING:
    from lancher_code.tools.context import ToolContext

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".lancher",
}

UI_PATH_LIMIT = 200
MODEL_PATH_LIMIT = 800
MODEL_MATCH_LIMIT = 400
MODEL_TEXT_CHAR_LIMIT = 24000






def atomic_write_text(
    path: Path, content: str, context: ToolContext, *,
    expected_exists: bool | None = None, expected_mtime_ns: int | None = None,
) -> None:
    """替换是文件提交边界：取消和过期版本只在提交前阻止写入。"""
    original = path.resolve()

    def check_before_commit() -> None:
        if context.cancellation_token is not None and context.cancellation_token.is_cancelled:
            raise asyncio.CancelledError
        if context.execution_runtime is not None and not context.execution_runtime.is_current(context.session_id, context.generation):
            raise asyncio.CancelledError
        try:
            writable = ensure_writable_path(path, context)
        except PathSandboxError as exc:
            raise PathWriteDeniedError("resource_target_changed", "写入期间文件路径已离开项目，未提交修改。") from exc
        if writable != original:
            raise PathWriteDeniedError("resource_target_changed", "写入期间文件目标发生变化，未提交修改。")
        exists = path.exists()
        if expected_exists is not None and exists != expected_exists:
            raise PathWriteDeniedError("file_changed_since_read", "写入期间目标文件被创建或删除，请重新读取后再写入。")
        if expected_mtime_ns is not None and (not exists or path.stat().st_mtime_ns != expected_mtime_ns):
            raise PathWriteDeniedError("file_changed_since_read", "文件在读取后已被修改，请重新读取最新内容后再写入。")

    check_before_commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    check_before_commit()
    previous_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        check_before_commit()
        # 这一行之后即为已提交；随后到达的停止不会把成功修改标记为撤销。
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # 清理失败不能覆盖原来的写入异常；临时文件不会成为目标文件。
            pass














def is_skipped_path(path: Path, root: Path) -> bool:
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def iter_files(root: Path, *, include: str | None = None) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        if include and not root.match(include):
            return []
        return [root.resolve()]

    files: list[Path] = []
    for path in root.rglob("*"):
        if is_skipped_path(path, root):
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        if not path.is_file():
            continue
        if include and not path.match(include):
            continue
        files.append(path.resolve())
    return files
