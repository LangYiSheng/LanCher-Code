from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ProviderProtocol = Literal["openai", "claude"]


@dataclass(slots=True)
class ThinkingConfig:
    enabled: bool = False
    budget_tokens: int | None = None

    @property
    def effective_budget_tokens(self) -> int:
        """容量预算与协议发送共享默认值，省略配置也不能预留成零。"""
        return self.budget_tokens if self.budget_tokens is not None else 2048


@dataclass(slots=True)
class ProviderConfig:
    protocol: ProviderProtocol
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    thinking: ThinkingConfig | None = None
    context_window: int = 128000


@dataclass(slots=True)
class ModelDefinition:
    """保存用户填写的模型配置；可选连接字段为 None 时继承供应商。"""

    model_name: str
    display_name: str = ""
    protocol: ProviderProtocol | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float | None = None
    context_window: int | None = None
    thinking: ThinkingConfig | None = None


@dataclass(slots=True)
class ProviderDefinition:
    name: str
    protocol: ProviderProtocol
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    models: dict[str, ModelDefinition] = field(default_factory=dict)
