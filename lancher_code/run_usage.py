from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import uuid4

from lancher_code.models import MessageUsage

UsageField = Literal["input", "output", "cache"]
RequestUsageStatus = Literal["running", "completed", "failed", "cancelled", "incomplete"]


class UsageObserver(Protocol):
    """接收实际模型请求的用量快照，不依赖对话消息或界面事件。"""

    def start_request(self, *, protocol: str, model: str) -> str: ...

    def update_request(
        self, request_id: str, usage: MessageUsage, *, provided_fields: frozenset[UsageField]
    ) -> None: ...

    def finish_request(self, request_id: str, *, status: RequestUsageStatus) -> None: ...


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

    @property
    def total_tokens(self) -> int:
        # 缓存命中是输入的子集，不能再相加一次。
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class _RequestUsage:
    protocol: str
    model: str
    usage: MessageUsage = field(default_factory=MessageUsage)
    provided_fields: frozenset[UsageField] = frozenset()
    status: RequestUsageStatus = "running"


class RunUsageTracker:
    """只统计本次启动的真实请求，恢复历史 Session 不会带入旧消耗。"""

    def __init__(self) -> None:
        self._requests: dict[str, _RequestUsage] = {}

    def start_request(self, *, protocol: str, model: str) -> str:
        request_id = uuid4().hex
        self._requests[request_id] = _RequestUsage(protocol=protocol, model=model)
        return request_id

    def update_request(
        self, request_id: str, usage: MessageUsage, *, provided_fields: frozenset[UsageField]
    ) -> None:
        request = self._requests[request_id]
        # 一个请求的 usage 是累计快照。复制后替换，避免末帧重复计数和外部修改。
        request.usage = MessageUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
        )
        request.provided_fields |= provided_fields

    def finish_request(self, request_id: str, *, status: RequestUsageStatus) -> None:
        self._requests[request_id].status = status

    def snapshot(self) -> RunUsageSummary:
        requests = tuple(self._requests.values())
        input_tokens = sum(request.usage.input_tokens for request in requests)
        output_tokens = sum(request.usage.output_tokens for request in requests)
        cached_input_tokens = sum(request.usage.cached_input_tokens for request in requests)
        input_count = sum("input" in request.provided_fields for request in requests)
        output_count = sum("output" in request.provided_fields for request in requests)
        cache_count = sum("cache" in request.provided_fields for request in requests)
        completed_count = sum(request.status == "completed" for request in requests)
        all_fields = frozenset({"input", "output", "cache"})
        incomplete_count = sum(
            request.status != "completed" or request.provided_fields != all_fields
            for request in requests
        )
        cache_complete = (
            bool(requests)
            and completed_count == len(requests)
            and input_count == len(requests)
            and cache_count == len(requests)
        )
        return RunUsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            request_count=len(requests),
            completed_request_count=completed_count,
            incomplete_request_count=incomplete_count,
            input_reported_request_count=input_count,
            output_reported_request_count=output_count,
            cache_reported_request_count=cache_count,
            cache_hit_ratio=cached_input_tokens / input_tokens if cache_complete and input_tokens else None,
        )
