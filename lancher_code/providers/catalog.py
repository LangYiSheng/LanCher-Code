from __future__ import annotations

import math
import os
import re
from copy import deepcopy
from collections.abc import Iterable, Mapping

from lancher_code.context.budget import context_budget
from lancher_code.errors import ConfigError
from lancher_code.providers.models import ModelDefinition, ProviderConfig, ProviderDefinition
from lancher_code.providers.metadata import ModelMetadata, bound_metadata, connection_fingerprint
from lancher_code.providers.reasoning import validate_effort, reasoning_parameters, resolve_api_mode


def iter_model_refs(providers: Mapping[str, ProviderDefinition]) -> list[str]:
    """按配置中的顺序列出稳定的供应商/模型引用。"""
    return [f"{provider_id}/{model_id}" for provider_id, provider in providers.items() for model_id in provider.models]


def _model_entry(providers: Mapping[str, ProviderDefinition], model_ref: str) -> tuple[str, ProviderDefinition, ModelDefinition]:
    reference = model_ref
    if not isinstance(reference, str) or reference.count("/") != 1:
        raise ConfigError("default_model 或模型引用必须采用 供应商ID/模型ID 格式。")
    provider_id, model_id = reference.split("/", 1)
    provider = providers.get(provider_id)
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


def resolve_model(providers: Mapping[str, ProviderDefinition], model_ref: str) -> ProviderConfig:
    """把模型覆盖与供应商默认值合并成独立连接快照，不改写原配置。"""
    reference, provider, model = _model_entry(providers, model_ref)
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
    model_name = _connection_string(model.model_name, f"{prefix}.model_name")
    metadata = bound_metadata(model.metadata, protocol=protocol, model=model_name, base_url=base_url, api_key=api_key)
    context_window = model.context_window if model.context_window is not None else (
        metadata.context_window if metadata and metadata.context_window else (200000 if protocol == "claude" else 128000))
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        raise ConfigError(f"{prefix}.context_window 必须是正整数。")
    max_output_tokens = model.max_output_tokens
    if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens <= 0):
        raise ConfigError(f"{prefix}.max_output_tokens 必须是正整数。")
    if metadata and metadata.max_output_tokens:
        if max_output_tokens is not None and max_output_tokens > metadata.max_output_tokens:
            raise ConfigError(f"{prefix}.max_output_tokens 超过端点声明的单次输出上限 {metadata.max_output_tokens}。")
        if max_output_tokens is None:
            max_output_tokens = min(context_budget(context_window).output_tokens, metadata.max_output_tokens)
    budget = context_budget(context_window, max_output_tokens)
    if max_output_tokens is not None and max_output_tokens >= context_window:
        raise ConfigError(f"{prefix}.max_output_tokens 必须小于上下文窗口 context_window。")
    validate_effort(protocol, metadata, model.reasoning_effort, path=f"{prefix}.reasoning_effort")
    reasoning_parameters(protocol=protocol, metadata=metadata,
                         effort=model.reasoning_effort, max_tokens=budget.output_tokens)
    return ProviderConfig(
        protocol=protocol,
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(timeout),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        reasoning_effort=model.reasoning_effort,
        metadata=metadata,
        api_mode=resolve_api_mode(protocol, metadata, model.api_mode if model.api_mode is not None else provider.api_mode),
    )


def model_display_name(providers: Mapping[str, ProviderDefinition], ref: str) -> str:
    """显示名不参与 API 请求；未填写时显示 API 模型名与供应商名称。"""
    reference, provider, model = _model_entry(providers, ref)
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


def models_on_connection(provider: ProviderDefinition, fingerprint: str) -> dict[str, list[str]]:
    """只认同一实际连接的 API 模型名；独立连接覆盖不被父供应商发现覆盖。"""
    matches: dict[str, list[str]] = {}
    for model_id, model in provider.models.items():
        protocol = model.protocol if model.protocol is not None else provider.protocol
        base_url = os.path.expandvars(model.base_url if model.base_url is not None else provider.base_url).strip().rstrip('/')
        api_key = os.path.expandvars(model.api_key if model.api_key is not None else provider.api_key).strip()
        if connection_fingerprint(protocol, base_url, api_key) == fingerprint:
            matches.setdefault(os.path.expandvars(model.model_name).strip(), []).append(model_id)
    return matches


def merge_discovered_models(provider: ProviderDefinition, discovered: Iterable[ModelMetadata],
                           selected: Iterable[str]) -> tuple[ProviderDefinition, int, int]:
    """批量加入所选模型并刷新同源已有模型的信息，保留稳定 ID 和全部手动覆盖。"""
    candidate = deepcopy(provider)
    fingerprint = connection_fingerprint(provider.protocol, os.path.expandvars(provider.base_url).strip().rstrip('/'),
                                         os.path.expandvars(provider.api_key).strip())
    records = {model.id: model for model in discovered}
    selected_ids = set(selected)
    if not selected_ids <= records.keys() or any(model.source_fingerprint != fingerprint for model in records.values()):
        raise ConfigError('模型发现结果与当前连接不一致，请重新发现后加入。')
    existing = models_on_connection(candidate, fingerprint)
    updated = added = 0
    for model_name, metadata in records.items():
        for model_id in existing.get(model_name, []):
            model = candidate.models[model_id]
            if model.metadata != metadata:
                model.metadata = metadata
                updated += 1
        if model_name in selected_ids and model_name not in existing:
            model_id = new_entry_id(model_name, candidate.models)
            candidate.models[model_id] = ModelDefinition(model_name=model_name, display_name=metadata.display_name,
                                                         metadata=metadata)
            added += 1
    return candidate, added, updated
