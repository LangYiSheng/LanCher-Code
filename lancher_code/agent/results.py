"""无界面运行与嵌入应用共享的结果契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lancher_code.contracts.json import json_value
from lancher_code.usage.models import MessageUsage


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: Literal["completed", "failed", "cancelled", "timeout"]
    session_id: str | None = None
    text: str = ""
    usage: MessageUsage = field(default_factory=MessageUsage)
    error_text: str | None = None
    run_usage: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return json_value(self)
