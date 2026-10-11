from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lancher_code.filesystem.access import PathWriteDeniedError
from lancher_code.sessions.paths import validate_session_id

if TYPE_CHECKING:
    from lancher_code.tools.context import ToolContext


MAX_DELETE_ENTRIES = 10_000


@dataclass(frozen=True, slots=True)
class DeleteEntry:
    path: Path
    kind: str
    fingerprint: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeletePlan:
    target: Path
    recursive: bool
    entries: tuple[DeleteEntry, ...]
    signature: str
    total_bytes: int
    is_directory: bool


class DeleteInterruptedError(PathWriteDeniedError):
    def __init__(self, reason_code: str, message: str, deleted_paths: tuple[Path, ...]) -> None:
        self.deleted_paths = deleted_paths
        super().__init__(reason_code, message)


def _check_cancelled(context: ToolContext) -> None:
    if context.cancellation_token is not None and context.cancellation_token.is_cancelled:
        raise asyncio.CancelledError
    if context.execution_runtime is not None and not context.execution_runtime.is_current(context.session_id, context.generation):
        raise asyncio.CancelledError


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_file_attributes", 0),
            getattr(info, "st_reparse_tag", 0))


def _entry(path: Path) -> DeleteEntry:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif path.is_junction():
        kind = "junction"
    elif getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        # 未知重解析点的删除语义不可靠，不能按普通目录遍历。
        raise PathWriteDeniedError("unsupported_delete_target", f"不支持删除这种重解析点：{path}")
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        raise PathWriteDeniedError("unsupported_delete_target", f"只能删除普通文件、目录或链接：{path}")
    return DeleteEntry(path, kind, _fingerprint(info))


def protected_delete_reason(path: Path, context: ToolContext, recursive: bool = True) -> str | None:
    """结构化删除和 Shell 共用的整体删除硬限制，不限制普通外部目录。"""
    root = (context.project_root or context.cwd).resolve()
    path = Path(os.path.abspath(path))
    if path == Path(path.anchor):
        return "delete_volume_root_protected"
    if path == root or (recursive and root.is_relative_to(path)):
        return "delete_root_protected"
    if recursive and path == Path.home().resolve():
        return "delete_home_protected"
    if os.name == "nt":
        windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
        protected = {windows_root, windows_root / "System32", windows_root / "SysWOW64"}
        protected.update(Path(value).resolve() for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData")
                         if (value := os.environ.get(name)))
    else:
        protected = {Path(value).resolve() for value in (
            "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/System", "/Library")}
    if recursive and any(path == item or item.is_relative_to(path) for item in protected):
        return "delete_system_directory_protected"
    sessions = root / ".lancher" / "sessions"
    if recursive and sessions.is_relative_to(path):
        return "session_records_protected"
    if path.is_relative_to(sessions):
        parts = path.relative_to(sessions).parts
        workspace_name = parts[1].casefold() if len(parts) >= 2 and os.name == "nt" else (parts[1] if len(parts) >= 2 else "")
        try:
            validate_session_id(parts[0] if parts else "")
            workspace_path = len(parts) >= 3 and workspace_name == "workspace"
        except ValueError:
            workspace_path = False
        if not workspace_path:
            return "session_records_protected"
    return None


def _validate_target(path: Path, context: ToolContext, *, recursive: bool = True) -> None:
    reason = protected_delete_reason(path, context, recursive)
    if reason:
        raise PathWriteDeniedError(reason, "不能整体删除项目、磁盘根目录、系统关键目录、用户主目录或会话内部记录。")
    if context.work_phase in {"discuss", "plan"}:
        raise PathWriteDeniedError("phase_disallowed", "讨论和计划阶段只读，不能删除文件或目录。")


def resolve_delete_target(raw_path: str, context: ToolContext) -> Path:
    """只解析父目录，末级链接删除的是链接本身，不解析其指向的目标。"""
    raw = Path(raw_path)
    if not raw.is_absolute():
        raw = context.cwd / raw
    lexical = Path(os.path.abspath(raw))
    _validate_target(lexical, context)
    path = lexical.parent.resolve() / lexical.name
    _validate_target(path, context)
    return path


def plan_delete(arguments: dict[str, object], context: ToolContext) -> DeletePlan:
    raw_path = arguments.get("path")
    recursive = arguments.get("recursive", False)
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise PathWriteDeniedError("invalid_arguments", "path 必须是非空文件路径。")
    if not isinstance(recursive, bool):
        raise PathWriteDeniedError("invalid_arguments", "recursive 必须是布尔值。")
    _check_cancelled(context)
    target = resolve_delete_target(raw_path, context)
    try:
        first = _entry(target)
    except FileNotFoundError as exc:
        raise PathWriteDeniedError("file_not_found", f"删除目标不存在：{target}") from exc
    except OSError as exc:
        raise PathWriteDeniedError("path_access_denied", f"无法检查删除目标：{exc}") from exc
    is_directory = first.kind == "directory"
    if is_directory and not recursive:
        raise PathWriteDeniedError("recursive_required", "删除目录必须显式设置 recursive=true，并确认实际删除清单。")
    entries: list[DeleteEntry] = []
    pending = [first]
    while pending:
        _check_cancelled(context)
        entry = pending.pop()
        _validate_target(entry.path, context)
        if entry.path.parent.resolve() != entry.path.parent:
            raise PathWriteDeniedError("delete_target_changed", "生成清单期间父目录被重定向，请重新请求删除。")
        entries.append(entry)
        if len(entries) > MAX_DELETE_ENTRIES:
            raise PathWriteDeniedError("delete_manifest_too_large", f"删除清单超过 {MAX_DELETE_ENTRIES} 项，请缩小删除范围。")
        if entry.kind == "directory":
            try:
                # 目录身份复核后才列出子项；链接和 junction 不会进入此分支。
                if _entry(entry.path) != entry or entry.path.parent.resolve() != entry.path.parent:
                    raise PathWriteDeniedError("delete_target_changed", "生成清单期间目录发生变化，请重新请求删除。")
                with os.scandir(entry.path) as iterator:
                    children = sorted((Path(item.path) for item in iterator), key=lambda value: str(value))
                if _entry(entry.path) != entry or entry.path.parent.resolve() != entry.path.parent:
                    raise PathWriteDeniedError("delete_target_changed", "生成清单期间目录发生变化，请重新请求删除。")
                pending.extend(_entry(child) for child in reversed(children))
            except OSError as exc:
                raise PathWriteDeniedError("path_access_denied", f"无法读取删除目录清单：{exc}") from exc
    # 使用完整清单和 lstat 版本绑定授权，任何增删、目标替换都改变签名。
    payload = [(str(entry.path), entry.kind, entry.fingerprint) for entry in entries]
    signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()
    return DeletePlan(target, recursive, tuple(entries), signature,
                      sum(entry.fingerprint[4] for entry in entries if entry.kind == "file"), is_directory)


async def execute_delete(plan: DeletePlan, context: ToolContext) -> tuple[Path, ...]:
    """提交前复核完整清单，逐项检查身份；中途失败明确报告已删除部分。"""
    _check_cancelled(context)
    latest = await asyncio.to_thread(plan_delete, {"path": str(plan.target), "recursive": plan.recursive}, context)
    if latest != plan:
        raise PathWriteDeniedError("delete_target_changed", "审批或等待期间删除目标发生变化，请重新请求删除。")
    deleted: list[Path] = []
    try:
        for entry in reversed(plan.entries):
            await asyncio.sleep(0)
            _check_cancelled(context)
            _validate_target(entry.path, context)
            if entry.path.parent.resolve() != entry.path.parent:
                raise PathWriteDeniedError("delete_target_changed", "删除期间父目录被重定向，已停止删除。")
            current = _entry(entry.path)
            # 删除子项会更新父目录时间，只保留目录自身的身份和类型检查。
            expected = entry.fingerprint
            actual = current.fingerprint
            unchanged = (current.kind == entry.kind and (
                actual[:3] == expected[:3] and actual[7:] == expected[7:]
                if entry.kind == "directory" else actual == expected))
            if not unchanged:
                raise PathWriteDeniedError("delete_target_changed", "删除期间目标发生变化，已停止删除。")
            if entry.kind in {"directory", "junction"}:
                entry.path.rmdir()
            else:
                entry.path.unlink()
            deleted.append(entry.path)
            if context.file_state_cache is not None:
                context.file_state_cache.discard(entry.path, recursive=entry.kind == "directory")
    except asyncio.CancelledError:
        if not deleted:
            raise
        raise DeleteInterruptedError("delete_interrupted", "删除已中断；部分目标已经删除。", tuple(deleted)) from None
    except (OSError, ValueError) as exc:
        code = exc.reason_code if isinstance(exc, PathWriteDeniedError) else "delete_error"
        if deleted:
            raise DeleteInterruptedError(code, f"删除未完成；部分目标已经删除：{exc}", tuple(deleted)) from exc
        if isinstance(exc, PathWriteDeniedError):
            raise
        raise PathWriteDeniedError(code, str(exc)) from exc
    return tuple(deleted)
