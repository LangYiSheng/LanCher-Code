from __future__ import annotations

import copy
import json
import logging
import os
import threading

from lancher_code.sessions.cache import (
    SessionSummary,
    events_prefix_digest,
    state_digest,
    summarize_events,
    write_cache,
    write_metadata,
)
from lancher_code.sessions.locking import SessionFileLock
from lancher_code.sessions.paths import SessionPaths
from lancher_code.sessions.storage import (
    EVENT_FORMAT_VERSION,
    SessionInfo,
    SessionRepositoryError,
    UnsupportedSessionFormatError,
    encode_json,
    normalize_title,
    parse_timestamp,
    reject_json_constant,
    unique_json_object,
    utc_timestamp,
    validate_storage_paths,
)


logger = logging.getLogger(__name__)

def read_events(paths: SessionPaths) -> tuple[list[dict], bytes, int]:
    """换行是记录提交边界；仅最后一段未换行的字节可以忽略。"""
    validate_storage_paths(paths)
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
                        line, parse_constant=reject_json_constant,
                        object_pairs_hook=unique_json_object,
                    )
                    if not isinstance(event, dict):
                        raise ValueError("记录不是 JSON 对象。")
                    if type(event.get("version")) is not int or event["version"] != EVENT_FORMAT_VERSION:
                        raise UnsupportedSessionFormatError(f"会话日志第 {number} 行：旧会话格式不兼容；原文件已保留，请创建新会话。")
                    if type(event.get("seq")) is not int or event["seq"] != len(records) + 1:
                        raise ValueError("事件序号不连续。")
                    parse_timestamp(event["timestamp"])
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
                        normalize_title(event["data"]["title"])
                        if not isinstance(event["data"]["initial_data"], dict):
                            raise ValueError("初始会话状态必须是 JSON 对象。")
                    records.append(event)
                except UnsupportedSessionFormatError:
                    raise
                except (KeyError, ValueError, TypeError, UnicodeError) as exc:
                    raise SessionRepositoryError(f"会话日志第 {number} 行无效：{exc}") from exc
                offset += len(line)
    except FileNotFoundError as exc:
        raise SessionRepositoryError(f"会话不存在：{paths.session_id}") from exc
    except OSError as exc:
        raise SessionRepositoryError(f"读取会话失败：{exc}") from exc
    if not records:
        raise SessionRepositoryError("会话日志缺少完整的创建记录。")
    validate_storage_paths(paths)
    return records, fragment, offset


class SessionWriter:
    """一个 Session 的唯一写入者；返回成功前事件已经 flush 和 fsync。"""

    def __init__(self, paths: SessionPaths, lock: SessionFileLock, events: list[dict]) -> None:
        self.paths = paths
        self.session_id = paths.session_id
        self._lock = lock
        self._summary = summarize_events(paths, events) if events else SessionSummary(paths.session_id)
        self._stream = paths.events.open("ab")
        self._mutex = threading.RLock()
        self._closed = False
        self._failed = False
        try:
            validate_storage_paths(paths)
        except Exception:
            self._stream.close()
            raise

    @property
    def info(self) -> SessionInfo:
        return self._summary.info()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_seq(self) -> int:
        return self._summary.last_seq

    def abort_creation(self) -> None:
        """关闭创建失败的日志句柄，保留锁供仓库安全清理未提交目录。"""
        with self._mutex:
            self._closed = True
            try:
                self._stream.close()
            except OSError as exc:
                logger.warning("未提交会话的事件句柄关闭失败：%s", exc)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionRepositoryError("会话写入者已关闭。")
        if self._failed:
            raise SessionRepositoryError("上次会话写入失败，请关闭并重新打开会话。")
        validate_storage_paths(self.paths)

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
            "timestamp": utc_timestamp(), "type": event_type, "data": data, "turn_id": turn_id,
        }
        # 编码后重新解析，调用方以后修改 data 不会影响已写入事件或元数据。
        payload = encode_json(event) + b"\n"
        event = json.loads(payload)
        probe = copy.copy(self._summary)
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
        self.refresh_metadata()
        return event

    def refresh_metadata(self) -> None:
        try:
            write_metadata(self.paths, self._summary)
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
            write_cache(self.paths, self.paths.checkpoint, {
                "version": EVENT_FORMAT_VERSION, "session_id": self.session_id,
                "last_seq": self._summary.last_seq, "state": state,
                "state_sha256": state_digest(state),
                "events_sha256": events_prefix_digest(self.paths, self._summary.last_seq),
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
