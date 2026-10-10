from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import uuid4


TokenEstimateSource = Literal["estimated", "usage_calibrated"]

CompactionTrigger = Literal["manual", "automatic", "emergency"]

CompactionStatus = Literal["running", "completed", "failed", "cancelled", "interrupted"]


@dataclass(slots=True)
class ContextUsageAnchor:
    token_count: int
    system_tools_digest: str
    message_count: int
    messages_digest: str


@dataclass(slots=True)
class ContextFileSnapshot:
    path: str
    normalized_path: str
    content: str
    read_at: str


@dataclass(slots=True)
class ContextManagementState:
    context_id: str = field(default_factory=lambda: uuid4().hex)
    usage_anchor: ContextUsageAnchor | None = None
    replacements: dict[str, str] = field(default_factory=dict)
    recent_files: list[ContextFileSnapshot] = field(default_factory=list)
    automatic_failure_count: int = 0
    automatic_compaction_disabled: bool = False


@dataclass(slots=True, frozen=True)
class ContextCompactionResult:
    before_tokens: int
    after_tokens: int
    dropped_groups: int = 0
    before_source: TokenEstimateSource = "estimated"
    after_source: TokenEstimateSource = "estimated"


@dataclass(slots=True)
class CompactionActivity:
    """一次完整压缩操作的展示事实，不进入模型上下文。"""

    id: str
    trigger: CompactionTrigger
    status: CompactionStatus
    started_at: datetime
    finished_at: datetime | None = None
    message_id: str | None = None
    after_message_id: str | None = None
    turn_id: str | None = None
    before_tokens: int | None = None
    after_tokens: int | None = None
    before_source: TokenEstimateSource | None = None
    after_source: TokenEstimateSource | None = None
    dropped_groups: int = 0
    error_text: str | None = None
    continued: bool = False
