from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from lancher_code.sessions.paths import (
    SessionPaths, validate_control_path, validate_session_id,
)


EVENT_FORMAT_VERSION = 1
logger = logging.getLogger(__name__)


class SessionRepositoryError(ValueError):
    """会话事件或存储无效，当前操作没有得到可靠的持久化结果。"""


class SessionBusyError(SessionRepositoryError):
    """同一会话已有一个持有操作系统文件锁的写入者。"""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    permission_rule_count: int
    model_ref: str | None
    archived: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionRepositoryError("会话标题必须是非空字符串。")
    return value.strip()


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串。")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区。")
    return result


def _validate_paths(paths: SessionPaths) -> None:
    try:
        paths.validate()
    except (OSError, ValueError) as exc:
        raise SessionRepositoryError(f"会话路径无效：{exc}") from exc


def _json_bytes(value: object, *, canonical: bool = False) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=canonical,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SessionRepositoryError(f"会话数据不能编码为 JSON：{exc}") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非法数值：{value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


class _FileLock:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self.stream: BinaryIO | None = None

    def acquire(self) -> None:
        _validate_paths(self.paths)
        try:
            self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
            _validate_paths(self.paths)
            stream = self.paths.lock.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise SessionBusyError("该 Session 正在使用，不能同时打开另一个写入者。") from exc
            self.stream = stream
        except SessionRepositoryError:
            raise
        except OSError as exc:
            raise SessionRepositoryError(f"无法锁定会话：{exc}") from exc

    def close(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _read_events(paths: SessionPaths) -> tuple[list[dict], bytes, int]:
    """换行是记录提交边界；仅最后一段未换行的字节可以忽略。"""
    _validate_paths(paths)
    records: list[dict] = []
    offset = 0
    fragment = b""
    try:
        with paths.events.open("rb") as stream:
            for number, line in enumerate(stream, 1):
                if not line.endswith(b"\n"):
                    fragment = line
                    break
                try:
                    event = json.loads(
                        line, parse_constant=_reject_constant,
                        object_pairs_hook=_unique_object,
                    )
                    if not isinstance(event, dict):
                        raise ValueError("记录不是 JSON 对象。")
                    if type(event.get("version")) is not int or event["version"] != EVENT_FORMAT_VERSION:
                        raise ValueError("不支持的事件格式版本。")
                    if type(event.get("seq")) is not int or event["seq"] != len(records) + 1:
                        raise ValueError("事件序号不连续。")
                    _datetime(event["timestamp"])
                    if not isinstance(event["type"], str) or not event["type"].strip():
                        raise ValueError("事件类型无效。")
                    if not isinstance(event["data"], dict):
                        raise ValueError("事件 data 必须是 JSON 对象。")
                    turn_id = event["turn_id"]
                    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
                        raise ValueError("事件 turn_id 无效。")
                    if (not records) != (event["type"] == "session.created"):
                        raise ValueError("首条记录必须是唯一的 session.created。")
                    if not records:
                        _title(event["data"]["title"])
                        if not isinstance(event["data"]["initial_data"], dict):
                            raise ValueError("初始会话状态必须是 JSON 对象。")
                    records.append(event)
                except (KeyError, ValueError, TypeError, UnicodeError) as exc:
                    raise SessionRepositoryError(f"会话日志第 {number} 行无效：{exc}") from exc
                offset += len(line)
    except FileNotFoundError as exc:
        raise SessionRepositoryError(f"会话不存在：{paths.session_id}") from exc
    except OSError as exc:
        raise SessionRepositoryError(f"读取会话失败：{exc}") from exc
    if not records:
        raise SessionRepositoryError("会话日志缺少完整的创建记录。")
    _validate_paths(paths)
    return records, fragment, offset


class _Summary:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.title = ""
        self.created_at = ""
        self.updated_at = ""
        self.message_ids: set[str] = set()
        self.permission_rule_count = 0
        self.model_ref: str | None = None
        self.archived = False
        self.last_seq = 0

    def apply(self, event: dict) -> None:
        data = event["data"]
        kind = event["type"]
        if kind == "session.created":
            initial = data["initial_data"]
            if not isinstance(initial, dict):
                raise ValueError("初始会话状态必须是 JSON 对象。")
            self.title = _title(data["title"])
            self.created_at = event["timestamp"]
            self._model(initial.get("model_ref"))
            self._rules(initial.get("rules", []))
            messages = initial.get("messages", [])
            if not isinstance(messages, list):
                raise ValueError("初始消息必须是列表。")
            for message in messages:
                self._message(message)
        elif kind == "message.created":
            self._message(data)
        elif kind == "permissions.changed":
            self._rules(data["rules"])
        elif kind == "model.changed":
            self._model(data["model_ref"])
        elif kind == "session.renamed":
            self.title = _title(data["title"])
        elif kind == "session.archived":
            self.archived = True
        self.updated_at = event["timestamp"]
        self.last_seq = event["seq"]

    def _message(self, message: dict) -> None:
        if not isinstance(message, dict):
            raise ValueError("消息必须是 JSON 对象。")
        if message.get("role") in {"user", "assistant"}:
            if not isinstance(message.get("id"), str) or not message["id"].strip():
                raise ValueError("消息 ID 必须是非空字符串。")
            self.message_ids.add(message["id"])

    def _rules(self, rules: object) -> None:
        if not isinstance(rules, list):
            raise ValueError("权限规则必须是列表。")
        self.permission_rule_count = len(rules)

    def _model(self, model_ref: object) -> None:
        if model_ref is not None and (not isinstance(model_ref, str) or not model_ref.strip()):
            raise ValueError("模型引用必须是非空字符串或 null。")
        self.model_ref = model_ref

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id, title=self.title,
            created_at=_datetime(self.created_at), updated_at=_datetime(self.updated_at),
            message_count=len(self.message_ids), permission_rule_count=self.permission_rule_count,
            model_ref=self.model_ref, archived=self.archived,
        )


def _summarize(paths: SessionPaths, events: list[dict]) -> _Summary:
    summary = _Summary(paths.session_id)
    try:
        for event in events:
            summary.apply(event)
        summary.info()
    except (KeyError, ValueError, TypeError) as exc:
        raise SessionRepositoryError(f"会话元数据无效：{exc}") from exc
    return summary


def _write_cache(paths: SessionPaths, path: Path, value: dict) -> None:
    _validate_paths(paths)
    validate_control_path(paths.project_root, path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _validate_paths(paths)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_metadata(paths: SessionPaths, summary: _Summary) -> None:
    value = asdict(summary.info())
    value["created_at"] = summary.created_at
    value["updated_at"] = summary.updated_at
    stat = paths.events.stat()
    value.update(version=1, last_seq=summary.last_seq, events_size=stat.st_size,
                 events_mtime_ns=stat.st_mtime_ns)
    _write_cache(paths, paths.metadata, value)


def _cached_info(paths: SessionPaths) -> SessionInfo | None:
    _validate_paths(paths)
    try:
        value = json.loads(paths.metadata.read_bytes())
        stat = paths.events.stat()
        if (value["version"] != 1 or value["session_id"] != paths.session_id
                or value["events_size"] != stat.st_size
                or value["events_mtime_ns"] != stat.st_mtime_ns):
            return None
        if any(type(value[key]) is not int or value[key] < 0 for key in ("message_count", "permission_rule_count")):
            return None
        if type(value["archived"]) is not bool or type(value["last_seq"]) is not int or value["last_seq"] < 1:
            return None
        model_ref = value["model_ref"]
        if model_ref is not None and (not isinstance(model_ref, str) or not model_ref.strip()):
            return None
        return SessionInfo(
            session_id=paths.session_id, title=_title(value["title"]),
            created_at=_datetime(value["created_at"]), updated_at=_datetime(value["updated_at"]),
            message_count=value["message_count"], permission_rule_count=value["permission_rule_count"],
            model_ref=model_ref, archived=value["archived"],
        )
    except (OSError, KeyError, ValueError, TypeError, UnicodeError):
        return None


def _prefix_digest(paths: SessionPaths, last_seq: int) -> str:
    """检查点绑定原始日志前缀，不能掩盖旧事件被改写的情况。"""
    _validate_paths(paths)
    digest = hashlib.sha256()
    with paths.events.open("rb") as stream:
        for _ in range(last_seq):
            line = stream.readline()
            if not line.endswith(b"\n"):
                raise ValueError("检查点指向尚未提交的事件。")
            digest.update(line)
    _validate_paths(paths)
    return digest.hexdigest()


def _state_digest(state: dict) -> str:
    return hashlib.sha256(_json_bytes(state, canonical=True)).hexdigest()


class SessionWriter:
    """一个 Session 的唯一写入者；返回成功前事件已经 flush 和 fsync。"""

    def __init__(self, paths: SessionPaths, lock: _FileLock, events: list[dict]) -> None:
        self.paths = paths
        self.session_id = paths.session_id
        self._lock = lock
        self._summary = _summarize(paths, events) if events else _Summary(paths.session_id)
        self._stream = paths.events.open("ab")
        self._mutex = threading.RLock()
        self._closed = False
        self._failed = False
        try:
            _validate_paths(paths)
        except Exception:
            self._stream.close()
            raise

    @property
    def info(self) -> SessionInfo:
        return self._summary.info()

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionRepositoryError("会话写入者已关闭。")
        if self._failed:
            raise SessionRepositoryError("上次会话写入失败，请关闭并重新打开会话。")
        _validate_paths(self.paths)

    def append(self, event_type: str, data: dict, turn_id: str | None = None) -> dict:
        # 同一个写入者也可能从主协程和上下文压缩线程同时被调用。
        with self._mutex:
            return self._append(event_type, data, turn_id)

    def _append(self, event_type: str, data: dict, turn_id: str | None = None) -> dict:
        self._ensure_open()
        if not isinstance(event_type, str) or not event_type.strip() or not isinstance(data, dict):
            raise SessionRepositoryError("事件类型必须是非空字符串，data 必须是 JSON 对象。")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
            raise SessionRepositoryError("事件 turn_id 必须是非空字符串或 null。")
        if (self._summary.last_seq == 0) != (event_type == "session.created"):
            raise SessionRepositoryError("首条事件必须是唯一的 session.created。")
        event = {
            "version": EVENT_FORMAT_VERSION, "seq": self._summary.last_seq + 1,
            "timestamp": _now(), "type": event_type, "data": data, "turn_id": turn_id,
        }
        # 编码后重新解析，调用方以后修改 data 不会影响已写入事件或元数据。
        payload = _json_bytes(event) + b"\n"
        event = json.loads(payload)
        probe = _Summary(self.session_id)
        probe.__dict__.update(self._summary.__dict__)
        probe.message_ids = set(self._summary.message_ids)
        try:
            probe.apply(event)
            probe.info()
        except (KeyError, ValueError, TypeError) as exc:
            raise SessionRepositoryError(f"事件内容无效：{exc}") from exc
        try:
            self._stream.write(payload)
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except OSError as exc:
            self._failed = True
            raise SessionRepositoryError(f"保存会话事件失败：{exc}") from exc
        self._summary = probe
        self._cache_metadata()
        return event

    def _cache_metadata(self) -> None:
        try:
            _write_metadata(self.paths, self._summary)
        except (OSError, ValueError, TypeError) as exc:
            # 事件才是真实记录；缓存失败不能把已提交的事件伪装成失败。
            logger.warning("会话元数据缓存写入失败，后续可重建：%s", exc)

    def flush(self) -> None:
        with self._mutex:
            self._flush()

    def _flush(self) -> None:
        self._ensure_open()
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except OSError as exc:
            self._failed = True
            raise SessionRepositoryError(f"刷新会话事件失败：{exc}") from exc

    def checkpoint(self, state: dict) -> None:
        with self._mutex:
            self._checkpoint(state)

    def _checkpoint(self, state: dict) -> None:
        self._ensure_open()
        try:
            if not isinstance(state, dict):
                raise ValueError("检查点状态必须是 JSON 对象。")
            _write_cache(self.paths, self.paths.checkpoint, {
                "version": 1, "session_id": self.session_id,
                "last_seq": self._summary.last_seq, "state": state,
                "state_sha256": _state_digest(state),
                "events_sha256": _prefix_digest(self.paths, self._summary.last_seq),
            })
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("会话检查点缓存写入失败，后续可重建：%s", exc)

    def close(self) -> None:
        with self._mutex:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        finally:
            self._lock.close()

    def __enter__(self) -> SessionWriter:
        self._ensure_open()
        return self

    def __exit__(self, *args) -> None:
        self.close()


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
        title = _title(title)
        if not isinstance(initial_data, dict):
            raise SessionRepositoryError("初始会话状态必须是 JSON 对象。")
        _json_bytes(initial_data)
        # 参数错误在分配会话目录之前拒绝，不能留下没有创建事件的空会话。
        _summarize(paths, [{"type": "session.created", "seq": 1, "timestamp": _now(),
                            "data": {"title": title, "initial_data": initial_data}}])
        lock = _FileLock(paths)
        lock.acquire()
        writer = None
        created_root = False
        succeeded = False
        try:
            _validate_paths(paths)
            if paths.root.exists():
                raise SessionRepositoryError(f"会话已存在：{session_id}")
            paths.root.mkdir()
            created_root = True
            paths.workspace.mkdir()
            paths.blobs.mkdir()
            _validate_paths(paths)
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
                        writer._closed = True
                        try:
                            writer._stream.close()
                        except OSError as exc:
                            logger.warning("未提交会话的事件句柄关闭失败：%s", exc)
                    if created_root and (writer is None or writer._summary.last_seq == 0):
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
        lock = _FileLock(paths)
        lock.acquire()
        writer = None
        try:
            events, fragment, offset = _read_events(paths)
            _summarize(paths, events)
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
                _validate_paths(paths)
                with paths.events.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            _validate_paths(paths)
            writer = SessionWriter(paths, lock, events)
            writer._cache_metadata()
            return writer
        except SessionRepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise SessionRepositoryError(f"打开会话失败：{exc}") from exc
        finally:
            if writer is None:
                lock.close()

    def read(self, session_id: str) -> list[dict]:
        events, _, _ = _read_events(self.paths(session_id))
        return events

    def load_checkpoint(self, session_id: str) -> dict | None:
        """缓存失效返回 None；日志完整行损坏仍必须明确报错。"""
        paths = self.paths(session_id)
        if not paths.checkpoint.is_file():
            return None
        events, _, _ = _read_events(paths)
        try:
            value = json.loads(paths.checkpoint.read_bytes(), parse_constant=_reject_constant,
                               object_pairs_hook=_unique_object)
            if (not isinstance(value, dict) or type(value["version"]) is not int
                    or value["version"] != 1 or value["session_id"] != session_id):
                return None
            seq, state = value["last_seq"], value["state"]
            if type(seq) is not int or not 1 <= seq <= len(events) or not isinstance(state, dict):
                return None
            if value["state_sha256"] != _state_digest(state):
                return None
            if value["events_sha256"] != _prefix_digest(paths, seq):
                return None
            _validate_paths(paths)
            return {"last_seq": seq, "state": state}
        except (OSError, KeyError, ValueError, TypeError, UnicodeError):
            return None

    def list_sessions(self) -> list[SessionInfo]:
        try:
            validate_control_path(self.project_root, self.session_dir)
            if not self.session_dir.exists():
                return []
            result = []
            for root in self.session_dir.iterdir():
                try:
                    validate_session_id(root.name)
                except ValueError:
                    continue
                paths = self.paths(root.name)
                if not paths.events.is_file():
                    continue
                info = _cached_info(paths)
                if info is None:
                    events, fragment, _ = _read_events(paths)
                    summary = _summarize(paths, events)
                    info = summary.info()
                    if not fragment:
                        # 读列表不等待活动写入者；只有拿到锁才能重建缓存，
                        # 避免把旧摘要与更新后的日志文件 stat 组合成假新缓存。
                        lock = _FileLock(paths)
                        try:
                            try:
                                lock.acquire()
                            except SessionBusyError:
                                pass
                            else:
                                try:
                                    events, fragment, _ = _read_events(paths)
                                    summary = _summarize(paths, events)
                                    info = summary.info()
                                    if not fragment:
                                        _write_metadata(paths, summary)
                                finally:
                                    lock.close()
                        except (OSError, ValueError, TypeError) as exc:
                            logger.warning("会话列表元数据缓存重建失败：%s", exc)
                result.append(info)
            return sorted(result, key=lambda item: item.updated_at, reverse=True)
        except SessionRepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise SessionRepositoryError(f"列出会话失败：{exc}") from exc

    def rename(self, session_id: str, title: str) -> None:
        title = _title(title)
        with self.open(session_id) as writer:
            writer.append("session.renamed", {"title": title})

    def archive(self, session_id: str) -> None:
        with self.open(session_id) as writer:
            writer.append("session.archived", {})

    def remove(self, session_id: str) -> None:
        paths = self.paths(session_id)
        lock = _FileLock(paths)
        lock.acquire()
        try:
            _validate_paths(paths)
            if not paths.events.is_file():
                raise SessionRepositoryError(f"会话不存在：{session_id}")
            # 已经复核固定 UUID 根目录，rmtree 不跟随内部符号链接。
            shutil.rmtree(paths.root)
        except OSError as exc:
            raise SessionRepositoryError(f"删除会话失败：{exc}") from exc
        finally:
            lock.close()
