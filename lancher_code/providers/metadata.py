"""模型信息的协议投影与落盘格式；不通过品牌、型号或域名猜测能力。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from lancher_code.errors import ConfigError

POSITIVE_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
API_MODES = ('auto', 'chat_completions', 'chat_completions_legacy', 'responses')


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    # None 表示未声明，空元组表示明确没有；不能把未知当成不支持。
    thinking_types: tuple[str, ...] | None = None
    effort_levels: tuple[str, ...] | None = None
    default_effort: str | None = None
    endpoints: tuple[str, ...] | None = None
    # 兼容端点可声明 Responses 的显式前缀缓存扩展，未知时不发送。
    prompt_cache_breakpoints: bool | None = None


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    id: str
    source_fingerprint: str
    fetched_at: str
    display_name: str = ''
    owned_by: str = ''
    created_at: str = ''
    lifecycle: str = ''
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)


def connection_fingerprint(protocol: str, base_url: str, api_key: str) -> str:
    """能力快照绑定实际连接及认证身份；不保存额外的密钥原文。"""
    data = json.dumps([protocol, base_url.rstrip('/'), api_key], ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def bound_metadata(metadata: ModelMetadata | None, *, protocol: str, model: str,
                   base_url: str, api_key: str) -> ModelMetadata | None:
    if metadata is not None and metadata.id == model and metadata.source_fingerprint == connection_fingerprint(protocol, base_url, api_key):
        return metadata
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _positive(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _strings(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return tuple(dict.fromkeys(item.strip() for item in value))


def _support(value: Any) -> bool | None:
    if isinstance(value, dict):
        value = value.get('supported')
    return value if type(value) is bool else None


def parse_model_metadata(raw: dict[str, Any], *, fingerprint: str, fetched_at: str) -> ModelMetadata:
    """标准 Claude 能力字段及兼容端点声明的通用扩展，共用同一解析器。"""
    model_id = _text(raw.get('id'))
    if not model_id or len(model_id) > 512 or any(ord(char) < 32 for char in model_id):
        raise ValueError('模型条目缺少有效的 id。')
    capabilities = raw.get('capabilities')
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    thinking = capabilities.get('thinking')
    types = None
    if _support(thinking) is False:
        types = ()
    elif isinstance(thinking, dict) and isinstance(thinking.get('types'), dict):
        types = tuple(name for name in ('adaptive', 'enabled', 'disabled')
                      if _support(thinking['types'].get(name)) is True)
        if not types and not any(_support(value) is False for value in thinking['types'].values()):
            types = None
    effort = capabilities.get('effort', raw.get('effort'))
    levels = None
    default = None
    if _support(effort) is False:
        levels = ()
    elif isinstance(effort, dict):
        declared = _strings(effort.get('supported_levels'))
        if declared is not None:
            levels = tuple(level for level in declared if level not in {'auto', 'none', 'off', 'disabled'})
        elif any(name in effort for name in POSITIVE_EFFORTS):
            levels = tuple(name for name in POSITIVE_EFFORTS if _support(effort.get(name)) is True)
            if not levels and not any(_support(effort.get(name)) is False for name in POSITIVE_EFFORTS):
                levels = None
        candidate = _text(effort.get('default_level'))
        default = candidate if levels is not None and candidate in levels else None
    endpoints = _strings(raw.get('supported_endpoints'))
    if endpoints is not None:
        endpoints = tuple(name for name in endpoints if name in API_MODES[1:])
    caching = capabilities.get('prompt_caching')
    breakpoints = (_support(caching.get('explicit_breakpoints'))
                   if isinstance(caching, dict) and _support(caching) is not False else None)
    if _support(caching) is False:
        breakpoints = False
    created = raw.get('created_at', raw.get('created'))
    created = str(created) if type(created) is int else _text(created)
    return ModelMetadata(
        id=model_id, source_fingerprint=fingerprint, fetched_at=fetched_at,
        display_name=_text(raw.get('display_name')) or _text(raw.get('name')),
        owned_by=_text(raw.get('owned_by')), created_at=created, lifecycle=_text(raw.get('lifecycle')),
        context_window=_positive(raw.get('context_window')) or _positive(raw.get('max_input_tokens')),
        max_output_tokens=_positive(raw.get('max_output_tokens')) or _positive(raw.get('max_tokens')),
        input_modalities=_strings(raw.get('input_modalities')), output_modalities=_strings(raw.get('output_modalities')),
        capabilities=ModelCapabilities(types, levels, default, endpoints, breakpoints),
    )


def serialize_metadata(metadata: ModelMetadata) -> dict[str, Any]:
    data = asdict(metadata)
    for key in ('input_modalities', 'output_modalities'):
        if data[key] is not None:
            data[key] = list(data[key])
    for key in ('thinking_types', 'effort_levels', 'endpoints'):
        if data['capabilities'][key] is not None:
            data['capabilities'][key] = list(data['capabilities'][key])
    return data


def load_metadata(raw: Any, path: str) -> ModelMetadata | None:
    if raw is None:
        return None
    def invalid() -> None:
        raise ConfigError(f'{path} 模型信息快照格式无效；可移除 metadata 后重新发现模型。')
    if not isinstance(raw, dict):
        invalid()
    for key in ('id', 'source_fingerprint', 'fetched_at'):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            invalid()
    cap = raw.get('capabilities', {})
    if not isinstance(cap, dict):
        invalid()
    for key in ('thinking_types', 'effort_levels', 'endpoints'):
        if cap.get(key) is not None and _strings(cap[key]) is None:
            invalid()
    for key in ('context_window', 'max_output_tokens'):
        if raw.get(key) is not None and _positive(raw[key]) is None:
            invalid()
    for key in ('input_modalities', 'output_modalities'):
        if raw.get(key) is not None and _strings(raw[key]) is None:
            invalid()
    for key in ('display_name', 'owned_by', 'created_at', 'lifecycle'):
        if key in raw and not isinstance(raw[key], str):
            invalid()
    if cap.get('default_effort') is not None and not isinstance(cap['default_effort'], str):
        invalid()
    if cap.get('prompt_cache_breakpoints') is not None and type(cap['prompt_cache_breakpoints']) is not bool:
        invalid()
    return ModelMetadata(
        id=raw['id'], source_fingerprint=raw['source_fingerprint'], fetched_at=raw['fetched_at'],
        **{key: raw.get(key, '') for key in ('display_name', 'owned_by', 'created_at', 'lifecycle')},
        context_window=raw.get('context_window'), max_output_tokens=raw.get('max_output_tokens'),
        input_modalities=_strings(raw.get('input_modalities')), output_modalities=_strings(raw.get('output_modalities')),
        capabilities=ModelCapabilities(*(_strings(cap.get(key)) for key in ('thinking_types', 'effort_levels')),
                                       cap.get('default_effort'), _strings(cap.get('endpoints')),
                                       cap.get('prompt_cache_breakpoints')),
    )
