from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lancher_code.config_system.loader import load_config_data
from lancher_code.config_system.paths import get_global_config_path
from lancher_code.models import AppConfig


def serialize_config(config: AppConfig) -> dict[str, Any]:
    provider_data: dict[str, Any] = {
        "protocol": config.provider.protocol,
        "model": config.provider.model,
        "base_url": config.provider.base_url,
        "api_key": config.provider.api_key,
        "timeout_seconds": config.provider.timeout_seconds,
        "context_window": config.provider.context_window,
    }
    if config.provider.thinking is not None:
        thinking_data: dict[str, Any] = {"enabled": config.provider.thinking.enabled}
        if config.provider.thinking.budget_tokens is not None:
            thinking_data["budget_tokens"] = config.provider.thinking.budget_tokens
        provider_data["thinking"] = thinking_data

    return {
        "provider": provider_data,
        "ui": {
            "show_timestamps": config.ui.show_timestamps,
            "show_thinking_status": config.ui.show_thinking_status,
        },
        "runtime": {
            "tool_loop_limit": config.runtime.tool_loop_limit,
            "unknown_tool_streak_limit": config.runtime.unknown_tool_streak_limit,
            "plan_file_path": config.runtime.plan_file_path,
            "permission_mode": config.runtime.permission_mode,
        },
    }


def write_config(path: str | Path, config: AppConfig) -> Path:
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.safe_dump(
        serialize_config(config),
        allow_unicode=True,
        sort_keys=False,
    )
    target_path.write_text(yaml_text, encoding="utf-8")
    return target_path


def write_config_data(path: str | Path, raw_data: dict[str, Any]) -> AppConfig:
    config = load_config_data(raw_data)
    write_config(path, config)
    return config


def write_global_config(config: AppConfig, *, home_dir: Path | None = None) -> Path:
    return write_config(get_global_config_path(home_dir), config)
