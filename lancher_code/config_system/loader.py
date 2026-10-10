from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml

from lancher_code.errors import ConfigError
from lancher_code.execution.contracts import CommandProfile, ExecutionConfig, ExecutionLimits, ReadinessProbe, ResourceClaim
from lancher_code.model_catalog import iter_model_refs, resolve_model
from lancher_code.models import (
    AppConfig,
    ModelDefinition,
    ProviderDefinition,
    ProviderProtocol,
    RuntimeConfig,
    RuntimeMode,
    ThinkingConfig,
    UIConfig,
    resolve_runtime_axes,
)

SUPPORTED_PROTOCOLS: tuple[ProviderProtocol, ...] = ("openai", "claude")
SUPPORTED_RUNTIME_MODES: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")

    try:
        raw_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法的 YAML: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件: {config_path}") from exc

    return load_config_data(raw_data)


def load_config_data(raw_data: Any) -> AppConfig:
    if raw_data is None:
        raise ConfigError("配置文件内容不能为空。")
    if not isinstance(raw_data, dict):
        raise ConfigError("配置文件顶层必须是对象。")

    if "provider" in raw_data and "providers" in raw_data:
        raise ConfigError("provider 与 providers 不能同时存在，请只保留一种配置格式。")
    ui_data = raw_data.get("ui", {})
    runtime_data = raw_data.get("runtime", {})

    if ui_data is None:
        ui_data = {}
    if runtime_data is None:
        runtime_data = {}
    if not isinstance(ui_data, dict):
        raise ConfigError("ui 配置必须是对象。")
    if not isinstance(runtime_data, dict):
        raise ConfigError("runtime 配置必须是对象。")

    legacy_format = "provider" in raw_data
    if legacy_format:
        provider_data = _require_mapping(raw_data, "provider")
        protocol = _require_protocol(provider_data, "protocol", "provider.protocol")
        providers = {
            "legacy": ProviderDefinition(
                name="原有供应商",
                protocol=protocol,
                base_url=_require_non_empty_string(provider_data, "base_url", "provider.base_url"),
                api_key=_require_non_empty_string(provider_data, "api_key", "provider.api_key"),
                timeout_seconds=_read_positive_float(provider_data.get("timeout_seconds", 60.0), "provider.timeout_seconds"),
                models={
                    "default": ModelDefinition(
                        model_name=_require_non_empty_string(provider_data, "model", "provider.model"),
                        context_window=_read_positive_int(provider_data["context_window"], "provider.context_window") if "context_window" in provider_data else None,
                        thinking=_load_thinking(provider_data.get("thinking"), "provider.thinking"),
                    )
                },
            )
        }
        default_model = "legacy/default"
    else:
        providers_data = _require_mapping(raw_data, "providers")
        if not providers_data:
            raise ConfigError("providers 至少需要一个供应商。")
        providers = {}
        for provider_id, data in providers_data.items():
            _require_entry_id(provider_id, "providers")
            providers[provider_id] = _load_provider(data, f"providers.{provider_id}")
        default_model = _require_non_empty_string(raw_data, "default_model")

    config = AppConfig(
        providers=providers,
        default_model=default_model,
        legacy_format=legacy_format,
        ui=_load_ui(ui_data),
        runtime=_load_runtime(runtime_data),
        execution=_load_execution(raw_data.get('execution', {})),
    )
    # 检查全部模型，而不只是默认模型；只生成快照，不把继承值填回目录。
    for reference in iter_model_refs(config):
        resolve_model(config, reference)
    config.provider = resolve_model(config)
    return config


def _require_entry_id(value: Any, path: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[\w-]+", value):
        raise ConfigError(f"{path} 的 ID 只能包含中文、字母、数字、下划线和短横线。")


def _load_execution(raw: Any) -> ExecutionConfig:
    if not isinstance(raw, dict):
        raise ConfigError('execution 必须是对象。')
    limits_raw = raw.get('limits', {})
    if not isinstance(limits_raw, dict):
        raise ConfigError('execution.limits 必须是对象。')
    defaults = ExecutionLimits()
    values = {}
    for key in ('max_concurrency', 'max_processes', 'max_processes_per_session',
                'output_limit_bytes', 'max_read_chars'):
        values[key] = _read_positive_int(limits_raw.get(key, getattr(defaults, key)), f'execution.limits.{key}')
    for key in ('stop_grace_seconds', 'drain_timeout_seconds'):
        values[key] = _read_positive_float(limits_raw.get(key, getattr(defaults, key)), f'execution.limits.{key}')
    if set(limits_raw) - set(values):
        raise ConfigError('execution.limits 含有未知配置项。')
    if values['max_processes_per_session'] > values['max_processes']:
        raise ConfigError('每个 Session 的进程上限不能超过应用进程上限。')
    if values['output_limit_bytes'] < 256:
        raise ConfigError('进程输出日志配额至少为 256 字节。')
    if values['max_read_chars'] > 100000:
        raise ConfigError('单次进程读取最多为 100000 字符。')
    profiles_raw = raw.get('command_profiles', [])
    if not isinstance(profiles_raw, list):
        raise ConfigError('execution.command_profiles 必须是数组。')
    profiles, names = [], set()
    for index, item in enumerate(profiles_raw):
        label = f'execution.command_profiles[{index}]'
        if not isinstance(item, dict):
            raise ConfigError(f'{label} 必须是对象。')
        if set(item) - {'name', 'command_match', 'resources', 'readiness'}:
            raise ConfigError(f'{label} 含有未知配置项。')
        name = _require_non_empty_string(item, 'name', f'{label}.name')
        if name in names:
            raise ConfigError(f'{label}.name 重复。')
        names.add(name)
        match = _require_non_empty_string(item, 'command_match', f'{label}.command_match')
        if 'resources' not in item:
            raise ConfigError(f'{label}.resources 必须明确填写；无资源需求请写 []。')
        resources = item['resources']
        if not isinstance(resources, list):
            raise ConfigError(f'{label}.resources 必须是数组。')
        claims = []
        for claim in resources:
            if not isinstance(claim, dict) or claim.get('kind') not in {'path', 'process', 'project', 'external'}:
                raise ConfigError(f'{label}.resources.kind 无效。')
            if set(claim) - {'kind', 'key', 'mode', 'recursive'}:
                raise ConfigError(f'{label}.resources 含有未知配置项。')
            key = _require_non_empty_string(claim, 'key', f'{label}.resources.key')
            mode, recursive = claim.get('mode', 'exclusive'), claim.get('recursive', False)
            if mode not in {'shared', 'exclusive'} or type(recursive) is not bool:
                raise ConfigError(f'{label}.resources 的 mode 或 recursive 无效。')
            if claim['kind'] != 'path' and recursive:
                raise ConfigError('recursive 只适用于路径资源。')
            claims.append(ResourceClaim(claim['kind'], key, mode, recursive))
        probe_raw = item.get('readiness')
        probe = None
        if probe_raw is not None:
            if not isinstance(probe_raw, dict) or probe_raw.get('kind', 'tcp') != 'tcp':
                raise ConfigError(f'{label}.readiness 只支持 TCP。')
            if set(probe_raw) - {'kind', 'host', 'port', 'timeout_ms'}:
                raise ConfigError(f'{label}.readiness 含有未知配置项。')
            host = probe_raw.get('host', '127.0.0.1')
            if host not in {'127.0.0.1', '::1', 'localhost'}:
                raise ConfigError('就绪探针只能访问本机回环地址。')
            port = _read_positive_int(probe_raw.get('port'), f'{label}.readiness.port')
            if port > 65535:
                raise ConfigError('就绪探针端口不能超过 65535。')
            timeout = _read_positive_int(probe_raw.get('timeout_ms', 30000), f'{label}.readiness.timeout_ms')
            probe = ReadinessProbe(host=host, port=port, timeout_ms=timeout)
        profiles.append(CommandProfile(name, match, tuple(claims), probe))
    if set(raw) - {'limits', 'command_profiles'}:
        raise ConfigError('execution 含有未知配置项。')
    return ExecutionConfig(ExecutionLimits(**values), profiles)


def _load_provider(raw: Any, path: str) -> ProviderDefinition:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 必须是对象。")
    models_data = raw.get("models", {})
    if not isinstance(models_data, dict):
        raise ConfigError(f"{path}.models 必须是对象。")
    models: dict[str, ModelDefinition] = {}
    for model_id, data in models_data.items():
        _require_entry_id(model_id, f"{path}.models")
        models[model_id] = _load_model(data, f"{path}.models.{model_id}")
    return ProviderDefinition(
        name=_require_non_empty_string(raw, "name", f"{path}.name"),
        protocol=_require_protocol(raw, "protocol", f"{path}.protocol"),
        base_url=_require_non_empty_string(raw, "base_url", f"{path}.base_url"),
        api_key=_require_non_empty_string(raw, "api_key", f"{path}.api_key"),
        timeout_seconds=_read_positive_float(raw.get("timeout_seconds", 60.0), f"{path}.timeout_seconds"),
        models=models,
    )


def _load_model(raw: Any, path: str) -> ModelDefinition:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} 必须是对象。")
    display_name = raw.get("display_name", "")
    if not isinstance(display_name, str):
        raise ConfigError(f"{path}.display_name 必须是字符串。")
    return ModelDefinition(
        model_name=_require_non_empty_string(raw, "model_name", f"{path}.model_name"),
        display_name=display_name.strip(),
        protocol=_require_protocol(raw, "protocol", f"{path}.protocol") if raw.get("protocol") is not None else None,
        base_url=_require_non_empty_string(raw, "base_url", f"{path}.base_url") if raw.get("base_url") is not None else None,
        api_key=_require_non_empty_string(raw, "api_key", f"{path}.api_key") if raw.get("api_key") is not None else None,
        timeout_seconds=_read_positive_float(raw["timeout_seconds"], f"{path}.timeout_seconds") if raw.get("timeout_seconds") is not None else None,
        context_window=_read_positive_int(raw["context_window"], f"{path}.context_window") if raw.get("context_window") is not None else None,
        thinking=_load_thinking(raw.get("thinking"), f"{path}.thinking"),
    )


def _load_thinking(raw_value: Any, path: str = "thinking") -> ThinkingConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ConfigError(f"{path} 配置必须是对象。")

    enabled = raw_value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{path}.enabled 必须是布尔值。")
    budget_tokens_raw = raw_value.get("budget_tokens")
    budget_tokens: int | None = None
    if budget_tokens_raw is not None:
        if isinstance(budget_tokens_raw, bool) or not isinstance(budget_tokens_raw, int) or budget_tokens_raw <= 0:
            raise ConfigError(f"{path}.budget_tokens 必须是正整数。")
        budget_tokens = budget_tokens_raw

    return ThinkingConfig(enabled=enabled, budget_tokens=budget_tokens)


def _load_ui(raw_value: dict[str, Any]) -> UIConfig:
    theme = raw_value.get("theme", "dark")
    busy_enter_action = raw_value.get("busy_enter_action", "follow_up")
    if not isinstance(theme, str) or theme not in {"dark", "light"}:
        raise ConfigError("ui.theme 必须是 dark 或 light。")
    if not isinstance(busy_enter_action, str) or busy_enter_action not in {"follow_up", "steer", "draft"}:
        raise ConfigError("ui.busy_enter_action 必须是 follow_up、steer 或 draft。")
    return UIConfig(
        show_timestamps=bool(raw_value.get("show_timestamps", False)),
        show_thinking_status=bool(raw_value.get("show_thinking_status", True)),
        theme=theme,
        busy_enter_action=busy_enter_action,
    )


def _load_runtime(raw_value: dict[str, Any]) -> RuntimeConfig:
    tool_loop_limit = _read_positive_int(raw_value.get("tool_loop_limit", 50), "runtime.tool_loop_limit")
    unknown_tool_streak_limit = _read_positive_int(
        raw_value.get("unknown_tool_streak_limit", 3),
        "runtime.unknown_tool_streak_limit",
    )
    for key, choices in (("work_phase", {"discuss", "plan", "execute"}), ("permission_policy", {"default", "acceptEdits", "bypass"})):
        if key in raw_value and (not isinstance(raw_value[key], str) or raw_value[key] not in choices):
            raise ConfigError(f"runtime.{key} 配置无效。")
    permission_mode = (
        None if "work_phase" in raw_value and "permission_policy" in raw_value
        else _read_runtime_mode(raw_value.get("permission_mode", "default"), "runtime.permission_mode")
    )
    try:
        work_phase, permission_policy = resolve_runtime_axes(
            permission_mode, raw_value.get("work_phase"), raw_value.get("permission_policy")
        )
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"runtime 阶段或权限配置无效：{exc}") from exc
    return RuntimeConfig(
        tool_loop_limit=tool_loop_limit,
        unknown_tool_streak_limit=unknown_tool_streak_limit,
        work_phase=work_phase,
        permission_policy=permission_policy,
    )


def _require_mapping(raw_data: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw_data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} 配置缺失或格式不正确。")
    return value


def _require_protocol(raw_data: dict[str, Any], key: str, path: str | None = None) -> ProviderProtocol:
    value = _require_non_empty_string(raw_data, key, path).lower()
    if value not in SUPPORTED_PROTOCOLS:
        supported = ", ".join(SUPPORTED_PROTOCOLS)
        raise ConfigError(f"{path or key} 必须是以下值之一: {supported}")
    return value  # type: ignore[return-value]


def _require_non_empty_string(raw_data: dict[str, Any], key: str, path: str | None = None) -> str:
    value = raw_data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path or key} 是必填字符串。")
    return value.strip()


def _read_positive_float(raw_value: Any, key: str) -> float:
    if not isinstance(raw_value, bool) and isinstance(raw_value, (int, float)) and math.isfinite(raw_value) and raw_value > 0:
        return float(raw_value)
    raise ConfigError(f"{key} 必须是正数。")


def _read_positive_int(raw_value: Any, key: str) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value <= 0:
        raise ConfigError(f"{key} 必须是正整数。")
    return raw_value


def _read_runtime_mode(raw_value: Any, key: str) -> RuntimeMode:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigError(f"{key} 必须是非空字符串。")
    normalized = raw_value.strip()
    if normalized not in SUPPORTED_RUNTIME_MODES:
        supported = ", ".join(SUPPORTED_RUNTIME_MODES)
        raise ConfigError(f"{key} 必须是以下值之一: {supported}")
    return normalized
