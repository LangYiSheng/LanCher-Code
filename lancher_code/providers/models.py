from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from lancher_code.providers.metadata import ModelMetadata


ProviderProtocol = Literal["openai", "claude"]


@dataclass(slots=True)
class ProviderConfig:
    protocol: ProviderProtocol
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    reasoning_effort: str = "auto"
    context_window: int = 128000
    max_output_tokens: int | None = None
    metadata: ModelMetadata | None = None
    api_mode: str = "auto"


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
    max_output_tokens: int | None = None
    metadata: ModelMetadata | None = None
    api_mode: str | None = None


@dataclass(slots=True)
class ProviderDefinition:
    name: str
    protocol: ProviderProtocol
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    models: dict[str, ModelDefinition] = field(default_factory=dict)
    api_mode: str = "auto"
