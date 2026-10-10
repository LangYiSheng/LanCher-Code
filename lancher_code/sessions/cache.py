from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from lancher_code.sessions.paths import SessionPaths, validate_control_path
from lancher_code.sessions.storage import (
    EVENT_FORMAT_VERSION,
    SessionInfo,
    SessionRepositoryError,
    encode_json,
    normalize_title,
    parse_timestamp,
    validate_storage_paths,
)


class SessionSummary:
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
            self.title = normalize_title(data["title"])
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
            self.title = normalize_title(data["title"])
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
            created_at=parse_timestamp(self.created_at), updated_at=parse_timestamp(self.updated_at),
            message_count=len(self.message_ids), permission_rule_count=self.permission_rule_count,
            model_ref=self.model_ref, archived=self.archived,
        )


def summarize_events(paths: SessionPaths, events: list[dict]) -> SessionSummary:
    summary = SessionSummary(paths.session_id)
    try:
        for event in events:
            summary.apply(event)
        summary.info()
    except (KeyError, ValueError, TypeError) as exc:
        raise SessionRepositoryError(f"会话元数据无效：{exc}") from exc
    return summary


def write_cache(paths: SessionPaths, path: Path, value: dict) -> None:
    validate_storage_paths(paths)
    validate_control_path(paths.project_root, path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encode_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        validate_storage_paths(paths)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_metadata(paths: SessionPaths, summary: SessionSummary) -> None:
    value = asdict(summary.info())
    value["created_at"] = summary.created_at
    value["updated_at"] = summary.updated_at
    stat = paths.events.stat()
    value.update(version=EVENT_FORMAT_VERSION, last_seq=summary.last_seq, events_size=stat.st_size,
                 events_mtime_ns=stat.st_mtime_ns)
    write_cache(paths, paths.metadata, value)


def read_cached_info(paths: SessionPaths) -> SessionInfo | None:
    validate_storage_paths(paths)
    try:
        value = json.loads(paths.metadata.read_bytes())
        stat = paths.events.stat()
        if (value["version"] != EVENT_FORMAT_VERSION or value["session_id"] != paths.session_id
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
            session_id=paths.session_id, title=normalize_title(value["title"]),
            created_at=parse_timestamp(value["created_at"]), updated_at=parse_timestamp(value["updated_at"]),
            message_count=value["message_count"], permission_rule_count=value["permission_rule_count"],
            model_ref=model_ref, archived=value["archived"],
        )
    except (OSError, KeyError, ValueError, TypeError, UnicodeError):
        return None


def events_prefix_digest(paths: SessionPaths, last_seq: int) -> str:
    """检查点绑定原始日志前缀，不能掩盖旧事件被改写的情况。"""
    validate_storage_paths(paths)
    digest = hashlib.sha256()
    with paths.events.open("rb") as stream:
        for _ in range(last_seq):
            line = stream.readline()
            if not line.endswith(b"\n"):
                raise ValueError("检查点指向尚未提交的事件。")
            digest.update(line)
    validate_storage_paths(paths)
    return digest.hexdigest()


def state_digest(state: dict) -> str:
    return hashlib.sha256(encode_json(state, canonical=True)).hexdigest()
