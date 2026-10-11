from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING
from lancher_code.sessions.paths import validate_control_path, validate_session_id
if TYPE_CHECKING:
    from lancher_code.tools.context import ToolContext

class PathWriteDeniedError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def resolve_path(cwd: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def resolve_access_path(cwd: Path, raw_path: str, context: ToolContext) -> Path:
    """解析真实路径；访问范围由统一权限引擎决定。"""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise ValueError("路径必须是非空字符串，且不能包含空字符。")
    return resolve_path(cwd, raw_path)


def is_session_workspace_path(path: Path, context: ToolContext) -> bool:
    """授权根由运行时提供，并且不能被链接重定向到项目其他位置。"""
    if not context.session_id or context.session_root is None or context.session_workspace is None:
        return False
    project_root = (context.project_root or context.cwd).resolve()
    try:
        validate_session_id(context.session_id)
    except ValueError:
        return False
    expected_root = project_root / ".lancher" / "sessions" / context.session_id
    expected_workspace = expected_root / "workspace"
    if context.session_root != expected_root or context.session_workspace != expected_workspace:
        return False
    try:
        validate_control_path(project_root, expected_workspace)
    except ValueError:
        return False
    return path.resolve().is_relative_to(expected_workspace)


def ensure_writable_path(path: Path, context: ToolContext, *, allow_plan_file: bool = False) -> Path:
    """阶段与内部记录限制在权限确认前后都必须成立。"""
    project_root = (context.project_root or context.cwd).resolve()
    resolved = path.resolve()
    sessions_root = project_root / ".lancher" / "sessions"
    lexical_path = Path(os.path.abspath(path))
    for candidate in (lexical_path, resolved):
        if candidate.is_relative_to(sessions_root):
            parts = candidate.relative_to(sessions_root).parts
            workspace_name = parts[1].casefold() if len(parts) >= 2 and os.name == "nt" else (parts[1] if len(parts) >= 2 else "")
            session_directory = parts[0].casefold() if parts and os.name == "nt" else (parts[0] if parts else "")
            try:
                validate_session_id(session_directory)
                workspace_path = len(parts) >= 3 and workspace_name == "workspace"
            except ValueError:
                workspace_path = False
            if not workspace_path:
                raise PathWriteDeniedError("session_records_protected", "会话内部记录由应用管理，工具只能写入 workspace 工作目录。")
    if context.work_phase in {"discuss", "plan"}:
        dedicated_plan = (allow_plan_file and context.work_phase == "plan"
                          and context.plan_file_path is not None
                          and resolved == context.plan_file_path.resolve()
                          and is_session_workspace_path(resolved, context))
        if not dedicated_plan:
            raise PathWriteDeniedError("phase_disallowed", "讨论和计划阶段只读；计划正文必须使用专用计划文件工具。")
    try:
        file_info = resolved.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PathWriteDeniedError("path_access_denied", f"无法检查写入目标：{exc}") from exc
    else:
        # 硬链接不会改变规范路径，但原地写入会同时修改控制记录等别名。
        if stat.S_ISREG(file_info.st_mode) and file_info.st_nlink > 1:
            raise PathWriteDeniedError("hardlinked_file_protected", "写入目标存在多个硬链接，为保护其他文件和会话记录，禁止原地写入。")
    return resolved


def resolve_writable_path(cwd: Path, raw_path: str, context: ToolContext) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    return ensure_writable_path(path, context)


def relative_display_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())
