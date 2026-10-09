from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from lancher_code.config_system.loader import load_config_data
from lancher_code.config_system.paths import get_global_config_path
from lancher_code.errors import ConfigError
from lancher_code.models import AppConfig


def serialize_config(config: AppConfig) -> dict[str, Any]:
    providers_data: dict[str, Any] = {}
    for provider_id, provider in config.providers.items():
        models_data: dict[str, Any] = {}
        for model_id, model in provider.models.items():
            model_data: dict[str, Any] = {"model_name": model.model_name}
            if model.display_name:
                model_data["display_name"] = model.display_name
            # 缺省覆盖继续缺省，避免把继承结果固化为模型自己的配置。
            for key in ("protocol", "base_url", "api_key", "timeout_seconds", "context_window"):
                value = getattr(model, key)
                if value is not None:
                    model_data[key] = value
            if model.thinking is not None:
                thinking_data: dict[str, Any] = {"enabled": model.thinking.enabled}
                if model.thinking.budget_tokens is not None:
                    thinking_data["budget_tokens"] = model.thinking.budget_tokens
                model_data["thinking"] = thinking_data
            models_data[model_id] = model_data
        providers_data[provider_id] = {
            "name": provider.name,
            "protocol": provider.protocol,
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "timeout_seconds": provider.timeout_seconds,
            "models": models_data,
        }

    return {
        "providers": providers_data,
        "default_model": config.default_model,
        "ui": {
            "show_timestamps": config.ui.show_timestamps,
            "show_thinking_status": config.ui.show_thinking_status,
            "theme": config.ui.theme,
            "busy_enter_action": config.ui.busy_enter_action,
        },
        "runtime": {
            "tool_loop_limit": config.runtime.tool_loop_limit,
            "unknown_tool_streak_limit": config.runtime.unknown_tool_streak_limit,
            "plan_file_path": config.runtime.plan_file_path,
            "work_phase": config.runtime.work_phase,
            "permission_policy": config.runtime.permission_policy,
        },
    }


def backup_legacy_config(path: Path) -> None:
    """首次迁移旧格式时保留原字节；已有备份不会被后续保存覆盖。"""
    if not path.exists():
        return
    backup_path = path.with_suffix(path.suffix + ".bak")
    created = False
    try:
        original = path.read_bytes()
        raw = yaml.safe_load(original.decode("utf-8"))
        if not isinstance(raw, dict) or "provider" not in raw:
            return
        with backup_path.open("xb") as target:
            created = True
            target.write(original)
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        return
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        if created:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ConfigError(f"无法备份旧模型配置：{path}") from exc


def write_config(path: str | Path, config: AppConfig) -> Path:
    target_path = Path(path)
    data = serialize_config(config)
    load_config_data(data)
    temporary: Path | None = None
    try:
        yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        backup_legacy_config(target_path)
        handle, temporary_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(yaml_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target_path)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法保存配置文件：{target_path}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return target_path


def write_config_data(path: str | Path, raw_data: dict[str, Any]) -> AppConfig:
    config = load_config_data(raw_data)
    write_config(path, config)
    return config


def write_global_config(config: AppConfig, *, home_dir: Path | None = None) -> Path:
    return write_config(get_global_config_path(home_dir), config)
