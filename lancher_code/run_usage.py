from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Iterable, Literal, Protocol
from uuid import uuid4

from lancher_code.models import MessageUsage, USAGE_FIELD_ATTRIBUTES, add_usage, merge_usage

UsageField = Literal["input", "output", "cache", "cache_creation", "reasoning"]
RequestUsageStatus = Literal["running", "completed", "failed", "cancelled", "incomplete"]


class UsageObserver(Protocol):
    """接收真实请求的累计快照，归属信息随记录一起保存。"""

    def start_request(self, *, protocol: str, model: str, session_id: str | None = None,
                      turn_id: str | None = None, message_id: str | None = None,
                      purpose: str = "chat", request_id: str | None = None) -> str: ...

    def update_request(self, request_id: str, usage: MessageUsage, *,
                       provided_fields: frozenset[UsageField] | None = None) -> None: ...

    def finish_request(self, request_id: str, *, status: RequestUsageStatus) -> None: ...


@dataclass(slots=True)
class RequestUsageRecord:
    request_id: str
    run_id: str
    protocol: str
    model: str
    session_id: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    purpose: str = "chat"
    status: RequestUsageStatus = "running"
    usage: MessageUsage = field(default_factory=lambda: MessageUsage(is_final=False))

    def to_dict(self) -> dict[str, object]:
        return {"request_id": self.request_id, "run_id": self.run_id, "protocol": self.protocol,
                "model": self.model, "session_id": self.session_id, "turn_id": self.turn_id,
                "message_id": self.message_id, "purpose": self.purpose, "status": self.status,
                "usage": self.usage.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RequestUsageRecord:
        for name in ("request_id", "run_id", "protocol", "model", "purpose"):
            if not isinstance(data.get(name), str) or not data[name]:
                raise ValueError(f"请求用量记录的 {name} 无效。")
        for name in ("session_id", "turn_id", "message_id"):
            if data.get(name) is not None and not isinstance(data[name], str):
                raise ValueError(f"请求用量记录的 {name} 无效。")
        status = data.get("status")
        if not isinstance(status, str) or status not in {"running", "completed", "failed", "cancelled", "incomplete"}:
            raise ValueError("请求用量记录的结束状态无效。")
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("请求用量记录缺少用量快照。")
        return cls(request_id=data["request_id"], run_id=data["run_id"], protocol=data["protocol"],
                   model=data["model"], purpose=data["purpose"], status=status,
                   session_id=data.get("session_id"), turn_id=data.get("turn_id"),
                   message_id=data.get("message_id"), usage=MessageUsage.from_dict(usage))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RunUsageSummary:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    request_count: int
    completed_request_count: int
    incomplete_request_count: int
    input_reported_request_count: int
    output_reported_request_count: int
    cache_reported_request_count: int
    cache_hit_ratio: float | None
    cache_creation_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    cache_creation_reported_request_count: int = 0
    reasoning_reported_request_count: int = 0
    invalid_request_count: int = 0
    usage: MessageUsage = field(default_factory=MessageUsage)

    @property
    def total_tokens(self) -> int:
        # 缓存已经包含在输入中，推理已经包含在输出中。
        return self.input_tokens + self.output_tokens


def summarize_records(records: Iterable[RequestUsageRecord]) -> RunUsageSummary:
    """会话与本次启动使用相同聚合口径，未知分量只计入已知小计。"""
    requests = tuple(records)
    usage = add_usage(*(request.usage for request in requests))
    counts = {name: sum(name in request.usage.known_fields and name not in request.usage.partial_fields
                        for request in requests) for name in USAGE_FIELD_ATTRIBUTES}
    completed_count = sum(request.status == "completed" for request in requests)
    required = {"input", "output", "cache"}
    incomplete_count = sum(request.status != "completed" or not request.usage.is_final
                           or not request.usage.is_valid or not required <= request.usage.known_fields
                           or bool(required & request.usage.partial_fields) for request in requests)
    invalid_count = sum(not request.usage.is_valid for request in requests)
    cache_complete = (bool(requests) and completed_count == len(requests) and not invalid_count
                      and all(request.usage.is_final for request in requests)
                      and counts["input"] == len(requests) and counts["cache"] == len(requests))
    return RunUsageSummary(
        input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
        cached_input_tokens=usage.cached_input_tokens or 0, request_count=len(requests),
        completed_request_count=completed_count, incomplete_request_count=incomplete_count,
        input_reported_request_count=counts["input"], output_reported_request_count=counts["output"],
        cache_reported_request_count=counts["cache"],
        cache_hit_ratio=(usage.cached_input_tokens or 0) / usage.input_tokens
        if cache_complete and usage.input_tokens else None,
        cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
        reasoning_output_tokens=usage.reasoning_output_tokens or 0,
        cache_creation_reported_request_count=counts["cache_creation"],
        reasoning_reported_request_count=counts["reasoning"], invalid_request_count=invalid_count,
        usage=usage,
    )


class RunUsageTracker:
    """请求 ID 去重账本；恢复历史不会自动导入到本次启动的消耗。"""

    def __init__(self, *, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid4().hex
        self._requests: dict[str, RequestUsageRecord] = {}

    @property
    def records(self) -> tuple[RequestUsageRecord, ...]:
        return tuple(deepcopy(record) for record in self._requests.values())

    def start_request(self, *, protocol: str, model: str, session_id: str | None = None,
                      turn_id: str | None = None, message_id: str | None = None,
                      purpose: str = "chat", request_id: str | None = None) -> str:
        identity = request_id or uuid4().hex
        if identity not in self._requests:
            self._requests[identity] = RequestUsageRecord(
                request_id=identity, run_id=self.run_id, protocol=protocol, model=model,
                session_id=session_id, turn_id=turn_id, message_id=message_id, purpose=purpose)
        else:
            self._validate_identity(self._requests[identity], protocol=protocol, model=model,
                                    session_id=session_id, turn_id=turn_id, message_id=message_id,
                                    purpose=purpose)
            self.bind_request(identity, session_id=session_id, turn_id=turn_id,
                              message_id=message_id, purpose=purpose)
        return identity

    def bind_request(self, request_id: str, *, session_id: str | None = None,
                     turn_id: str | None = None, message_id: str | None = None,
                     purpose: str | None = None) -> None:
        request = self._requests[request_id]
        self._validate_identity(request, session_id=session_id, turn_id=turn_id,
                                message_id=message_id, purpose=purpose)
        for name, value in (("session_id", session_id), ("turn_id", turn_id),
                            ("message_id", message_id), ("purpose", purpose)):
            if value is not None:
                setattr(request, name, value)

    def update_request(self, request_id: str, usage: MessageUsage, *,
                       provided_fields: frozenset[UsageField] | None = None) -> None:
        request = self._requests[request_id]
        # None 自身表达未上报；覆盖集合不能把未知伪造成零。
        incoming = deepcopy(usage)
        if provided_fields is not None:
            for name, attribute in USAGE_FIELD_ATTRIBUTES.items():
                if name not in provided_fields:
                    setattr(incoming, attribute, None)
        request.usage = merge_usage(request.usage, incoming)

    def finish_request(self, request_id: str, *, status: RequestUsageStatus) -> None:
        self._requests[request_id].status = status

    def accept_record(self, record: RequestUsageRecord | dict[str, object]) -> None:
        incoming = RequestUsageRecord.from_dict(record.to_dict() if isinstance(record, RequestUsageRecord) else record)
        if incoming.run_id != self.run_id:
            raise ValueError("不能把其它启动的历史请求导入当前账本。")
        existing = self._requests.get(incoming.request_id)
        if existing is not None:
            self._validate_identity(existing, protocol=incoming.protocol, model=incoming.model,
                                    session_id=incoming.session_id, turn_id=incoming.turn_id,
                                    message_id=incoming.message_id, purpose=incoming.purpose)
        self._requests[incoming.request_id] = incoming

    @staticmethod
    def _validate_identity(record: RequestUsageRecord, **values: str | None) -> None:
        for name, value in values.items():
            existing = getattr(record, name)
            if value is not None and existing is not None and value != existing:
                raise ValueError(f"同一个请求 ID 不能跨 {name} 复用。")

    def snapshot(self) -> RunUsageSummary:
        return summarize_records(self._requests.values())
