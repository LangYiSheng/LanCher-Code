from __future__ import annotations

import textwrap

import pytest

from lancher_code.config import load_config
from lancher_code.errors import ConfigError


def test_load_config_success(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            provider:
              protocol: openai
              model: gpt-4.1-mini
              base_url: https://api.openai.com/v1
              api_key: ${TEST_OPENAI_KEY}
              timeout_seconds: 30
            ui:
              show_timestamps: true
              show_thinking_status: false
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_file))

    assert config.provider.protocol == "openai"
    assert config.provider.model == "gpt-4.1-mini"
    assert config.provider.base_url == "https://api.openai.com/v1"
    assert config.provider.api_key == "${TEST_OPENAI_KEY}"
    assert config.provider.timeout_seconds == 30.0
    assert config.ui.show_timestamps is True
    assert config.ui.show_thinking_status is False
    assert config.runtime.tool_loop_limit == 50


@pytest.mark.parametrize("field_name", ["protocol", "model", "base_url", "api_key"])
def test_load_config_rejects_missing_required_field(tmp_path, field_name: str) -> None:
    config_file = tmp_path / "config.yaml"
    raw_config = {
        "protocol": "openai",
        "model": "gpt-4.1-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key": "test-key",
    }
    del raw_config[field_name]

    config_file.write_text(
        "provider:\n"
        + "\n".join(f"  {key}: {value}" for key, value in raw_config.items()),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(str(config_file))

    assert field_name in exc_info.value.user_message


def test_load_config_rejects_invalid_protocol(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            provider:
              protocol: invalid
              model: gpt-4.1-mini
              base_url: https://example.com
              api_key: test-key
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(str(config_file))

    assert "protocol" in exc_info.value.user_message


def test_load_config_reads_thinking_defaults(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            provider:
              protocol: claude
              model: claude-sonnet
              base_url: https://api.anthropic.com/v1
              api_key: test-key
              thinking:
                enabled: true
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_file))

    assert config.provider.protocol == "claude"
    assert config.provider.thinking is not None
    assert config.provider.thinking.enabled is True
    assert config.provider.thinking.budget_tokens is None
    assert config.runtime.tool_loop_limit == 50


def test_load_config_reads_runtime_tool_loop_limit(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            provider:
              protocol: openai
              model: gpt-4.1-mini
              base_url: https://api.openai.com/v1
              api_key: test-key
            runtime:
              tool_loop_limit: 123
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_file))

    assert config.runtime.tool_loop_limit == 123


def test_load_config_reads_runtime_permission_mode(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            provider:
              protocol: openai
              model: gpt-4.1-mini
              base_url: https://api.openai.com/v1
              api_key: test-key
            runtime:
              permission_mode: acceptEdits
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_config(str(config_file))

    assert config.runtime.permission_mode == "acceptEdits"


def test_load_config_rejects_invalid_runtime_tool_loop_limit(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            provider:
              protocol: openai
              model: gpt-4.1-mini
              base_url: https://api.openai.com/v1
              api_key: test-key
            runtime:
              tool_loop_limit: 0
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(str(config_file))

    assert "runtime.tool_loop_limit" in exc_info.value.user_message


def test_load_config_rejects_invalid_yaml(tmp_path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("provider: [", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config(str(config_file))

    assert "YAML" in exc_info.value.user_message
