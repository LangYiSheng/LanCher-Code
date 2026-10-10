from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lancher_code.config.loader import load_config, load_config_data
from lancher_code.config.models import RuntimeConfig
from lancher_code.config.settings import SettingsError, SettingsService
from lancher_code.config.writer import serialize_config
from lancher_code.errors import ConfigError
from lancher_code.permissions.storage import PermissionStorage


def config_data() -> dict:
    return {
        "providers": {"test": {"name": "测试", "protocol": "openai", "base_url": "https://example.test/v1",
                               "api_key": "${TEST_KEY}", "models": {"main": {"model_name": "main"}}}},
        "default_model": "test/main",
    }


def make_service(tmp_path: Path) -> SettingsService:
    path = tmp_path / "lancher.yaml"
    path.write_text(yaml.safe_dump(config_data()), encoding="utf-8")
    return SettingsService(
        config_path=path, global_mcp_path=tmp_path / "global-mcp.yaml",
        project_mcp_path=tmp_path / "project-mcp.yaml",
        permission_storage=PermissionStorage(project_rules_path=tmp_path / "project-rules.yaml",
                                             user_rules_path=tmp_path / "user-rules.yaml"),
    )


def test_experimental_mcp_append_defaults_off() -> None:
    assert RuntimeConfig().experimental_mcp_tool_append is False
    assert load_config_data(config_data()).runtime.experimental_mcp_tool_append is False


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_experimental_mcp_append_rejects_nonbool_in_loader_and_constructor(value: object) -> None:
    data = config_data() | {"runtime": {"experimental_mcp_tool_append": value}}
    with pytest.raises(ConfigError, match="experimental_mcp_tool_append"):
        load_config_data(data)
    with pytest.raises(ValueError, match="experimental_mcp_tool_append"):
        RuntimeConfig(experimental_mcp_tool_append=value)


@pytest.mark.parametrize("enabled", [False, True])
def test_experimental_mcp_append_serializes_and_round_trips(enabled: bool) -> None:
    config = load_config_data(config_data() | {"runtime": {"experimental_mcp_tool_append": enabled}})
    serialized = serialize_config(config)
    assert serialized["runtime"]["experimental_mcp_tool_append"] is enabled
    assert load_config_data(serialized).runtime.experimental_mcp_tool_append is enabled


def test_runtime_save_preserves_latest_other_domains_and_environment_references(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    stale = service.load().config
    current = service.load().config
    current.providers["test"].models["main"].model_name = "latest"
    service.save_models(current)
    current.ui.theme = "light"
    service.save_ui(current.ui)
    stale.runtime.experimental_mcp_tool_append = True
    committed = service.save_runtime(stale.runtime)

    assert committed.runtime.experimental_mcp_tool_append is True
    assert committed.providers["test"].models["main"].model_name == "latest"
    assert committed.providers["test"].api_key == "${TEST_KEY}"
    assert committed.ui.theme == "light"
    assert load_config(service.config_path).runtime.experimental_mcp_tool_append is True
    assert not service.global_mcp_path.exists() and not service.project_mcp_path.exists()
    assert not service.permission_storage.project_rules_path.exists()


def test_runtime_save_rejects_invalid_mutation_before_writing(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    before = service.config_path.read_bytes()
    runtime = service.load().config.runtime
    runtime.experimental_mcp_tool_append = "false"
    with pytest.raises(SettingsError, match="experimental_mcp_tool_append"):
        service.save_runtime(runtime)
    assert service.config_path.read_bytes() == before
