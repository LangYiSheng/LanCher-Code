from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

from lancher_code.sessions.cache import events_prefix_digest, read_cached_info, state_digest, summarize_events
from lancher_code.sessions.event_log import SessionWriter, read_events
from lancher_code.sessions.locking import SessionFileLock
from lancher_code.sessions.paths import SessionPaths, validate_control_path, validate_session_id
from lancher_code.sessions.storage import (
    EVENT_FORMAT_VERSION,
    SessionInfo,
    SessionIssue,
    SessionListing,
    SessionRepositoryError,
    UnsupportedSessionFormatError,
    encode_json,
    normalize_title,
    reject_json_constant,
    unique_json_object,
    utc_timestamp,
    validate_storage_paths,
)


logger = logging.getLogger(__name__)

class ProjectSessionRepository:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.session_dir = self.project_root / ".lancher" / "sessions"

    def paths(self, session_id: str) -> SessionPaths:
        try:
            return SessionPaths.for_session(self.project_root, session_id)
        except (OSError, ValueError) as exc:
            raise SessionRepositoryError(f"会话路径无效：{exc}") from exc

    def create(self, session_id: str, title: str, initial_data: dict) -> SessionWriter:
        paths = self.paths(session_id)
        title = normalize_title(title)
        if not isinstance(initial_data, dict):
            raise SessionRepositoryError("初始会话状态必须是 JSON 对象。")
        encode_json(initial_data)
        # 参数错误在分配会话目录之前拒绝，不能留下没有创建事件的空会话。
        summarize_events(paths, [{"type": "session.created", "seq": 1, "timestamp": utc_timestamp(),
                            "data": {"title": title, "initial_data": initial_data}}])
        lock = SessionFileLock(paths)
        lock.acquire()
        writer = None
        created_root = False
        succeeded = False
        try:
            validate_storage_paths(paths)
            if paths.root.exists():
                raise SessionRepositoryError(f"会话已存在：{session_id}")
            paths.root.mkdir()
            created_root = True
            paths.workspace.mkdir()
            paths.blobs.mkdir()
            validate_storage_paths(paths)
            writer = SessionWriter(paths, lock, [])
            writer.append("session.created", {"title": title, "initial_data": initial_data})
            succeeded = True
            return writer
        except OSError as exc:
            raise SessionRepositoryError(f"创建会话失败：{exc}") from exc
        finally:
            if not succeeded:
                try:
                    if writer is not None:
                        # 先关闭事件句柄，但在清理完成前保留外部 OS 锁，
                        # 防止另一个写入者打开尚未完成创建的目录。
                        writer.abort_creation()
                    if created_root and (writer is None or writer.last_seq == 0):
                        self._discard_uncommitted_creation(paths)
                finally:
                    lock.close()

    def _discard_uncommitted_creation(self, paths: SessionPaths) -> None:
        """只清理本次分配且尚未提交创建事件的固定 UUID 根目录。"""
        try:
            # 清理检查根及祖先的真实绝对位置；内部链接只会被 unlink，
            # shutil.rmtree 不跟随 symlink 或 Windows junction。
            validate_control_path(paths.project_root, paths.root)
            if not paths.root.exists():
                return
            try:
                shutil.rmtree(paths.root)
            except OSError:
                # 删除受阻时保留失败现场，但移出正常 UUID 列表并允许重试。
                failed = paths.root.with_name(f".failed-{paths.session_id}-{uuid4().hex}")
                validate_control_path(paths.project_root, paths.root)
                validate_control_path(paths.project_root, failed)
                paths.root.rename(failed)
        except (OSError, ValueError) as exc:
            # 保留原始创建异常，不以清理异常覆盖它；路径被重定向时不操作。
            logger.warning("未提交会话目录无法清理或隔离：%s", exc)

    def open(self, session_id: str) -> SessionWriter:
        paths = self.paths(session_id)
        if not paths.events.is_file():
            raise SessionRepositoryError(f"会话不存在：{session_id}")
        read_events(paths)
        lock = SessionFileLock(paths)
        lock.acquire()
        writer = None
        try:
            events, fragment, offset = read_events(paths)
            summarize_events(paths, events)
            if fragment:
                recovery = paths.root / "recovery"
                validate_control_path(paths.project_root, recovery)
                recovery.mkdir(exist_ok=True)
                validate_control_path(paths.project_root, recovery)
                saved = recovery / f"fragment-{uuid4().hex}.bin"
                with saved.open("xb") as stream:
                    stream.write(fragment)
                    stream.flush()
                    os.fsync(stream.fileno())
                validate_storage_paths(paths)
                with paths.events.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            validate_storage_paths(paths)
            writer = SessionWriter(paths, lock, events)
            writer.refresh_metadata()
            return writer
        except SessionRepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise SessionRepositoryError(f"打开会话失败：{exc}") from exc
        finally:
            if writer is None:
                lock.close()

    def read(self, session_id: str) -> list[dict]:
        events, _, _ = read_events(self.paths(session_id))
        return events

    def load_checkpoint(self, session_id: str) -> dict | None:
        """缓存失效返回 None；日志完整行损坏仍必须明确报错。"""
        paths = self.paths(session_id)
        if not paths.checkpoint.is_file():
            return None
        events, _, _ = read_events(paths)
        try:
            value = json.loads(paths.checkpoint.read_bytes(), parse_constant=reject_json_constant,
                               object_pairs_hook=unique_json_object)
            if (not isinstance(value, dict) or type(value["version"]) is not int
                    or value["version"] != EVENT_FORMAT_VERSION or value["session_id"] != session_id):
                return None
            seq, state = value["last_seq"], value["state"]
            if type(seq) is not int or not 1 <= seq <= len(events) or not isinstance(state, dict):
                return None
            if value["state_sha256"] != state_digest(state):
                return None
            if value["events_sha256"] != events_prefix_digest(paths, seq):
                return None
            validate_storage_paths(paths)
            return {"last_seq": seq, "state": state}
        except (OSError, KeyError, ValueError, TypeError, UnicodeError):
            return None

    def list_sessions(self) -> SessionListing:
        """列表只读；单个旧格式或损坏记录不遮蔽其它有效会话。"""
        items: list[SessionInfo] = []
        issues: list[SessionIssue] = []
        try:
            validate_control_path(self.project_root, self.session_dir)
            if not self.session_dir.exists():
                return SessionListing(items, issues)
            for root in self.session_dir.iterdir():
                try:
                    validate_session_id(root.name)
                except ValueError:
                    continue
                try:
                    paths = self.paths(root.name)
                    events, _, _ = read_events(paths)
                    info = read_cached_info(paths) or summarize_events(paths, events).info()
                    items.append(info)
                except UnsupportedSessionFormatError as exc:
                    issues.append(SessionIssue(root.name, "unsupported_format", str(exc)))
                except (SessionRepositoryError, OSError, ValueError, TypeError) as exc:
                    issues.append(SessionIssue(root.name, "invalid_data", str(exc)))
            return SessionListing(sorted(items, key=lambda item: item.updated_at, reverse=True), issues)
        except (OSError, ValueError) as exc:
            raise SessionRepositoryError(f"列出会话失败：{exc}") from exc

    def rename(self, session_id: str, title: str) -> None:
        title = normalize_title(title)
        with self.open(session_id) as writer:
            writer.append("session.renamed", {"title": title})

    def archive(self, session_id: str) -> None:
        with self.open(session_id) as writer:
            writer.append("session.archived", {})

    def remove(self, session_id: str) -> None:
        paths = self.paths(session_id)
        lock = SessionFileLock(paths)
        lock.acquire()
        try:
            validate_storage_paths(paths)
            if not paths.events.is_file():
                raise SessionRepositoryError(f"会话不存在：{session_id}")
            # 已经复核固定 UUID 根目录，rmtree 不跟随内部符号链接。
            shutil.rmtree(paths.root)
        except OSError as exc:
            raise SessionRepositoryError(f"删除会话失败：{exc}") from exc
        finally:
            lock.close()
