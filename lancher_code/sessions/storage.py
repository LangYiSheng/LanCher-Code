from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from lancher_code.sessions.paths import SessionPaths


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


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionRepositoryError("会话标题必须是非空字符串。")
    return value.strip()


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串。")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区。")
    return result


def validate_storage_paths(paths: SessionPaths) -> None:
    try:
        paths.validate()
    except (OSError, ValueError) as exc:
        raise SessionRepositoryError(f"会话路径无效：{exc}") from exc


def encode_json(value: object, *, canonical: bool = False) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=canonical,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SessionRepositoryError(f"会话数据不能编码为 JSON：{exc}") from exc


def reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 包含非法数值：{value}")


def unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


EVENT_FORMAT_VERSION = 2


class UnsupportedSessionFormatError(SessionRepositoryError):
    """保留旧日志，当前程序只接受本次开发格式。"""


@dataclass(frozen=True, slots=True)
class SessionIssue:
    session_id: str
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class SessionListing:
    items: list[SessionInfo]
    issues: list[SessionIssue]
