from __future__ import annotations

import pytest
import yaml

from lancher_code.config.loader import load_config, load_config_data
from lancher_code.errors import ConfigError
from lancher_code.providers.catalog import resolve_model


def _data(protocol="openai"):
    return {
        "providers": {"main": {
            "name": "测试供应商", "protocol": protocol,
            "base_url": "https://api.openai.com/v1", "api_key": "${TEST_OPENAI_KEY}",
            "timeout_seconds": 30, "models": {"default": {"model_name": "gpt-4.1-mini"}},
        }},
        "default_model": "main/default",
    }


def _resolved(config):
    return resolve_model(config.providers, config.default_model)


def test_load_config_success(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_OPENAI_KEY", raising=False)
    raw = _data()
    raw["ui"] = {"show_timestamps": True, "show_thinking_status": False}
    path = tmp_path / "配置.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(path)
    provider = _resolved(config)
    assert provider.protocol == "openai"
    assert provider.model == "gpt-4.1-mini"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.api_key == "${TEST_OPENAI_KEY}"
    assert provider.timeout_seconds == 30.0
    assert config.ui.show_timestamps is True
    assert config.ui.show_thinking_status is False
    assert config.runtime.tool_loop_limit == 50


@pytest.mark.parametrize("field_name", ["protocol", "name", "base_url", "api_key", "model_name"])
def test_load_config_rejects_missing_required_field(tmp_path, field_name: str) -> None:
    raw = _data()
    provider = raw["providers"]["main"]
    del (provider["models"]["default"] if field_name == "model_name" else provider)[field_name]
    path = tmp_path / "配置.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(path)
    assert field_name in exc_info.value.user_message
    assert str(path) in exc_info.value.user_message


def test_load_config_rejects_invalid_protocol() -> None:
    with pytest.raises(ConfigError, match="protocol"):
        load_config_data(_data("invalid"))


def test_load_config_reads_thinking_defaults() -> None:
    raw = _data("claude")
    raw["providers"]["main"]["models"]["default"]["thinking"] = {"enabled": True}
    config = load_config_data(raw)
    provider = _resolved(config)
    assert provider.protocol == "claude"
    assert provider.thinking.enabled is True
    assert provider.thinking.budget_tokens is None
    assert config.runtime.tool_loop_limit == 50


def test_load_config_reads_runtime_axes_and_tool_loop_limit() -> None:
    raw = _data()
    raw["runtime"] = {"tool_loop_limit": 123, "work_phase": "plan", "permission_policy": "acceptEdits"}
    config = load_config_data(raw)
    assert config.runtime.tool_loop_limit == 123
    assert config.runtime.work_phase == "plan"
    assert config.runtime.permission_policy == "acceptEdits"


@pytest.mark.parametrize("runtime", [
    {"permission_mode": "acceptEdits"},
    {"permission_mode": "plan", "work_phase": "execute", "permission_policy": "default"},
])
def test_old_runtime_mode_is_rejected_even_with_current_axes(runtime) -> None:
    raw = _data()
    raw["runtime"] = runtime
    with pytest.raises(ConfigError, match="重新配置"):
        load_config_data(raw)


def test_load_config_rejects_invalid_runtime_tool_loop_limit() -> None:
    raw = _data()
    raw["runtime"] = {"tool_loop_limit": 0}
    with pytest.raises(ConfigError, match="runtime.tool_loop_limit"):
        load_config_data(raw)


def test_load_config_rejects_invalid_yaml(tmp_path) -> None:
    path = tmp_path / "配置.yaml"
    path.write_text("providers: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_provider_context_window_defaults_and_validates() -> None:
    assert _resolved(load_config_data(_data())).context_window == 128000
    assert _resolved(load_config_data(_data("claude"))).context_window == 200000
    for value in (True, 0, -1, "128000"):
        raw = _data()
        raw["providers"]["main"]["models"]["default"]["context_window"] = value
        with pytest.raises(ConfigError):
            load_config_data(raw)
