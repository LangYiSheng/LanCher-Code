from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SESSION_FORMAT_VERSION = 2
SUPPORTED_SESSION_FORMAT_VERSIONS = {1, SESSION_FORMAT_VERSION}
_VALID_NAME = re.compile(r"^[\w\-\u3400-\u9fff]+$", re.UNICODE)


class SessionStoreError(ValueError):
    """项目会话存储错误。"""


@dataclass(frozen=True, slots=True)
class StoredSessionInfo:
    name: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    permission_rule_count: int


class ProjectSessionStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.session_dir = self.project_root / ".lancher" / "session"

    def validate_name(self, name: str) -> str:
        normalized = name.strip()
        if normalized in {"", ".", ".."} or not _VALID_NAME.fullmatch(normalized):
            raise SessionStoreError("会话名称只能包含中文、字母、数字、下划线和短横线。")
        return normalized

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def save(self, name: str, records: list[dict[str, object]]) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise SessionStoreError(f"保存会话失败：{exc}") from exc
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def load(self, name: str) -> list[dict[str, object]]:
        path = self._path(name)
        if not path.is_file():
            raise SessionStoreError(f"会话不存在：{name}")
        records: list[dict[str, object]] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise SessionStoreError(f"第 {line_number} 行不是 JSON 对象。")
                    records.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SessionStoreError(f"读取会话失败：{exc}") from exc
        if not records:
            raise SessionStoreError("会话文件为空。")
        return records

    def list_sessions(self) -> list[StoredSessionInfo]:
        if not self.session_dir.is_dir():
            return []
        sessions: list[StoredSessionInfo] = []
        for path in self.session_dir.glob("*.jsonl"):
            try:
                records = self.load(path.stem)
                metadata = records[0]
                if metadata.get("type") != "metadata":
                    continue
                sessions.append(
                    StoredSessionInfo(
                        name=str(metadata["name"]),
                        created_at=_parse_datetime(metadata["created_at"]),
                        updated_at=_parse_datetime(metadata["updated_at"]),
                        message_count=int(metadata.get("message_count", 0)),
                        permission_rule_count=int(metadata.get("permission_rule_count", 0)),
                    )
                )
            except (KeyError, TypeError, ValueError, SessionStoreError):
                continue
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def remove(self, name: str) -> None:
        path = self._path(name)
        if not path.is_file():
            raise SessionStoreError(f"会话不存在：{name}")
        try:
            path.unlink()
        except OSError as exc:
            raise SessionStoreError(f"删除会话失败：{exc}") from exc

    def rename(self, old_name: str, new_name: str) -> None:
        source = self._path(old_name)
        target = self._path(new_name)
        if not source.is_file():
            raise SessionStoreError(f"会话不存在：{old_name}")
        if target.exists():
            raise SessionStoreError(f"会话名称已存在：{new_name}")
        records = self.load(old_name)
        metadata = records[0]
        if metadata.get("type") != "metadata":
            raise SessionStoreError("会话文件缺少 metadata。")
        metadata["name"] = self.validate_name(new_name)
        metadata["updated_at"] = utc_now().isoformat()
        self.save(new_name, records)
        try:
            source.unlink()
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise SessionStoreError(f"重命名会话失败：{exc}") from exc

    def _path(self, name: str) -> Path:
        return self.session_dir / f"{self.validate_name(name)}.jsonl"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("时间字段必须是字符串。")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间字段必须包含时区。")
    return parsed
