from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG


_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")


def validate_session_id(session_id: str) -> str:
    """磁盘主键只接受规范的小写 UUID hex，名称不能参与路径拼接。"""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Session ID 必须是 32 位小写 UUID hex。")
    return session_id


def validate_control_path(project_root: Path, path: Path) -> None:
    """拒绝会话控制路径中的符号链接、Windows junction 和越界路径。"""
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("会话路径越过项目边界。") from exc
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"会话控制路径不能使用符号链接或 junction：{current}")
        try:
            status = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        # 目录的 nlink 在 POSIX 上通常大于 1，只检查普通文件。
        # 硬链接不会改变 resolve()，却会让控制数据与其他文件共享同一内容。
        if S_ISREG(status.st_mode) and status.st_nlink > 1:
            raise ValueError(f"会话控制文件不能使用硬链接：{current}")
    if path.resolve() != path:
        raise ValueError(f"会话控制路径已被重定向：{path}")


@dataclass(frozen=True, slots=True)
class SessionPaths:
    project_root: Path
    session_id: str
    root: Path
    workspace: Path
    plan: Path
    events: Path
    metadata: Path
    checkpoint: Path
    blobs: Path
    lock: Path

    @classmethod
    def for_session(cls, project_root: Path, session_id: str) -> SessionPaths:
        session_id = validate_session_id(session_id)
        project = Path(project_root).resolve()
        directory = project / ".lancher" / "sessions"
        root = directory / session_id
        paths = cls(
            project_root=project, session_id=session_id, root=root,
            workspace=root / "workspace", plan=root / "workspace" / "plan.md",
            events=root / "events.jsonl", metadata=root / "meta.json",
            checkpoint=root / "checkpoint.json", blobs=root / "blobs",
            # 锁在会话目录外，Windows 可以在保持锁的同时删除整个会话目录。
            lock=directory / ".locks" / f"{session_id}.lock",
        )
        paths.validate()
        return paths

    def validate(self) -> None:
        validate_session_id(self.session_id)
        for path in (
            self.root, self.workspace, self.plan, self.events, self.metadata,
            self.checkpoint, self.blobs, self.lock,
        ):
            validate_control_path(self.project_root, path)
