from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable
from copy import deepcopy

from lancher_code.errors import ConfigError
from lancher_code.models import AppConfig, ModelDefinition, ProviderConfig, ProviderDefinition


def iter_model_refs(config: AppConfig) -> list[str]:
    """按配置中的顺序列出稳定的供应商/模型引用。"""
    return [f"{provider_id}/{model_id}" for provider_id, provider in config.providers.items() for model_id in provider.models]


def _model_entry(config: AppConfig, model_ref: str | None) -> tuple[str, ProviderDefinition, ModelDefinition]:
    reference = config.default_model if model_ref is None else model_ref
    if not isinstance(reference, str) or reference.count("/") != 1:
        raise ConfigError("default_model 或模型引用必须采用 供应商ID/模型ID 格式。")
    provider_id, model_id = reference.split("/", 1)
    provider = config.providers.get(provider_id)
    if provider is None or model_id not in provider.models:
        raise ConfigError(f"模型引用不存在：{reference}")
    return reference, provider, provider.models[model_id]


def _connection_string(value: str, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} 是必填字符串。")
    # 保持既有未定义变量原样保留的行为，只在调用前展开副本。
    expanded = os.path.expandvars(value).strip()
    if not expanded:
        raise ConfigError(f"{path} 展开环境变量后的值不能为空。")
    return expanded


def resolve_model(config: AppConfig, model_ref: str | None = None) -> ProviderConfig:
    """把模型覆盖与供应商默认值合并成独立连接快照，不改写原配置。"""
    reference, provider, model = _model_entry(config, model_ref)
    prefix = f"providers.{reference.replace('/', '.models.')}"
    protocol = model.protocol if model.protocol is not None else provider.protocol
    if protocol not in {"openai", "claude"}:
        raise ConfigError(f"{prefix}.protocol 必须是 openai 或 claude。")
    base_url = _connection_string(model.base_url if model.base_url is not None else provider.base_url, f"{prefix}.base_url").rstrip("/")
    if not base_url:
        raise ConfigError(f"{prefix}.base_url 不能为空。")
    api_key = _connection_string(model.api_key if model.api_key is not None else provider.api_key, f"{prefix}.api_key")
    timeout = model.timeout_seconds if model.timeout_seconds is not None else provider.timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError(f"{prefix}.timeout_seconds 必须是有限正数。")
    context_window = model.context_window if model.context_window is not None else (200000 if protocol == "claude" else 128000)
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        raise ConfigError(f"{prefix}.context_window 必须是正整数。")
    return ProviderConfig(
        protocol=protocol,
        model=_connection_string(model.model_name, f"{prefix}.model_name"),
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(timeout),
        context_window=context_window,
        thinking=deepcopy(model.thinking) if protocol == "claude" else None,
    )


def model_display_name(config: AppConfig, ref: str) -> str:
    """显示名不参与 API 请求；未填写时显示 API 模型名与供应商名称。"""
    reference, provider, model = _model_entry(config, ref)
    return model.display_name.strip() or f"{_connection_string(model.model_name, f'{reference}.model_name')} ({provider.name})"


def new_entry_id(name: str, existing_ids: Iterable[str]) -> str:
    """名称只在创建时生成 ID，之后改名不改变引用。"""
    base = re.sub(r"[^\w-]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-_") or "entry"
    existing = set(existing_ids)
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
