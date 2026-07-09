from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from lancher_code.errors import ConfigError
from lancher_code.models import (
    AppConfig,
    ProviderConfig,
    ProviderProtocol,
    RuntimeConfig,
    RuntimeMode,
    ThinkingConfig,
    UIConfig,
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

    provider_data = _require_mapping(raw_data, "provider")
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

    protocol = _require_protocol(provider_data, "protocol")
    model = _require_non_empty_string(provider_data, "model")
    base_url = _expand_env(_require_non_empty_string(provider_data, "base_url"))
    api_key = _expand_env(_require_non_empty_string(provider_data, "api_key"))
    timeout_seconds = _read_positive_float(provider_data.get("timeout_seconds", 60.0), "timeout_seconds")
    thinking = _load_thinking(provider_data.get("thinking"))
    ui = _load_ui(ui_data)
    runtime = _load_runtime(runtime_data)

    provider = ProviderConfig(
        protocol=protocol,
        model=_expand_env(model),
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        thinking=thinking,
    )
    return AppConfig(provider=provider, ui=ui, runtime=runtime)


def _load_thinking(raw_value: Any) -> ThinkingConfig | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ConfigError("thinking 配置必须是对象。")

    enabled = bool(raw_value.get("enabled", False))
    budget_tokens_raw = raw_value.get("budget_tokens")
    budget_tokens: int | None = None
    if budget_tokens_raw is not None:
        if not isinstance(budget_tokens_raw, int) or budget_tokens_raw <= 0:
            raise ConfigError("thinking.budget_tokens 必须是正整数。")
        budget_tokens = budget_tokens_raw

    return ThinkingConfig(enabled=enabled, budget_tokens=budget_tokens)


def _load_ui(raw_value: dict[str, Any]) -> UIConfig:
    return UIConfig(
        show_timestamps=bool(raw_value.get("show_timestamps", False)),
        show_thinking_status=bool(raw_value.get("show_thinking_status", True)),
    )


def _load_runtime(raw_value: dict[str, Any]) -> RuntimeConfig:
    tool_loop_limit = _read_positive_int(raw_value.get("tool_loop_limit", 50), "runtime.tool_loop_limit")
    unknown_tool_streak_limit = _read_positive_int(
        raw_value.get("unknown_tool_streak_limit", 3),
        "runtime.unknown_tool_streak_limit",
    )
    plan_file_path = raw_value.get("plan_file_path", "./.lancher/plan.md")
    permission_mode = _read_runtime_mode(raw_value.get("permission_mode", "default"), "runtime.permission_mode")
    if not isinstance(plan_file_path, str) or not plan_file_path.strip():
        raise ConfigError("runtime.plan_file_path 必须是非空字符串。")
    return RuntimeConfig(
        tool_loop_limit=tool_loop_limit,
        unknown_tool_streak_limit=unknown_tool_streak_limit,
        plan_file_path=plan_file_path.strip(),
        permission_mode=permission_mode,
    )


def _require_mapping(raw_data: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw_data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} 配置缺失或格式不正确。")
    return value


def _require_protocol(raw_data: dict[str, Any], key: str) -> ProviderProtocol:
    value = _require_non_empty_string(raw_data, key).lower()
    if value not in SUPPORTED_PROTOCOLS:
        supported = ", ".join(SUPPORTED_PROTOCOLS)
        raise ConfigError(f"{key} 必须是以下值之一: {supported}")
    return value  # type: ignore[return-value]


def _require_non_empty_string(raw_data: dict[str, Any], key: str) -> str:
    value = raw_data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 是必填字符串。")
    return value.strip()


def _read_positive_float(raw_value: Any, key: str) -> float:
    if isinstance(raw_value, (int, float)) and raw_value > 0:
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


def _expand_env(value: str) -> str:
    return os.path.expandvars(value)
