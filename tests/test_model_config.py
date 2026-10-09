from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lancher_code.config import load_config, load_config_data, serialize_config, write_config, write_config_data
from lancher_code.errors import ConfigError
from lancher_code.model_catalog import iter_model_refs, model_display_name, new_entry_id, resolve_model
from lancher_code.models import AppConfig
from lancher_code.permission_engine import PermissionStorage
from lancher_code.settings_service import SettingsError, SettingsService


def _catalog_data() -> dict:
    return {
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "protocol": "openai",
                "base_url": "https://example.test/v1/",
                "api_key": "${L_CODE_TEST_KEY}",
                "timeout_seconds": 25,
                "models": {
                    "chat": {"model_name": "deepseek-chat", "display_name": "日常模型"},
                    "reasoner": {"model_name": "deepseek-reasoner"},
                    "claude": {
                        "model_name": "claude-compatible",
                        "protocol": "claude",
                        "base_url": "https://claude.example.test/v1",
                        "api_key": "other-key",
                        "timeout_seconds": 45,
                        "thinking": {"enabled": True, "budget_tokens": 1024},
                    },
                },
            },
            "backup": {
                "name": "备用供应商",
                "protocol": "openai",
                "base_url": "https://backup.example.test/v1",
                "api_key": "backup-key",
                "models": {"chat": {"model_name": "deepseek-chat"}},
            },
        },
        "default_model": "deepseek/chat",
    }


def test_catalog_resolves_inheritance_overrides_and_display_names(monkeypatch) -> None:
    monkeypatch.setenv("L_CODE_TEST_KEY", "expanded-key")
    config = load_config_data(_catalog_data())

    default = resolve_model(config)
    assert default.model == "deepseek-chat"
    assert default.protocol == "openai"
    assert default.base_url == "https://example.test/v1"
    assert default.api_key == "expanded-key"
    assert default.timeout_seconds == 25.0
    assert default.context_window == 128000
    alternate = resolve_model(config, "deepseek/claude")
    assert alternate.protocol == "claude"
    assert alternate.base_url == "https://claude.example.test/v1"
    assert alternate.api_key == "other-key"
    assert alternate.timeout_seconds == 45.0
    assert alternate.context_window == 200000
    assert alternate.thinking is not None and alternate.thinking.budget_tokens == 1024
    assert model_display_name(config, "deepseek/chat") == "日常模型"
    assert model_display_name(config, "deepseek/reasoner") == "deepseek-reasoner (DeepSeek)"
    assert iter_model_refs(config) == ["deepseek/chat", "deepseek/reasoner", "deepseek/claude", "backup/chat"]


def test_resolved_snapshot_never_overwrites_raw_values_or_inheritance(monkeypatch) -> None:
    monkeypatch.setenv("L_CODE_TEST_KEY", "expanded-key")
    config = load_config_data(_catalog_data())
    original = serialize_config(config)
    config.provider.model = "not-persisted"
    snapshot = resolve_model(config, "deepseek/claude")
    snapshot.thinking.budget_tokens = 5
    assert serialize_config(config) == original
    assert original["providers"]["deepseek"]["api_key"] == "${L_CODE_TEST_KEY}"
    assert "api_key" not in original["providers"]["deepseek"]["models"]["chat"]
    config.providers["deepseek"].api_key = "changed-parent-key"
    assert resolve_model(config).api_key == "changed-parent-key"
    assert resolve_model(config, "deepseek/claude").api_key == "other-key"
    assert resolve_model(config, "deepseek/claude").thinking.budget_tokens == 1024


def test_legacy_loading_migrates_without_expanding_saved_environment(monkeypatch) -> None:
    monkeypatch.setenv("L_CODE_TEST_KEY", "expanded-key")
    config = load_config_data({"provider": {
        "protocol": "claude", "model": "old-model", "base_url": "https://old.test/v1/",
        "api_key": "${L_CODE_TEST_KEY}", "context_window": 100000,
        "thinking": {"enabled": True, "budget_tokens": 2048},
    }})
    assert config.legacy_format
    assert config.default_model == "legacy/default"
    assert config.provider.api_key == "expanded-key"
    serialized = serialize_config(config)
    assert "provider" not in serialized
    assert serialized["providers"]["legacy"]["api_key"] == "${L_CODE_TEST_KEY}"
    reloaded = load_config_data(serialized)
    assert not reloaded.legacy_format
    assert resolve_model(reloaded) == resolve_model(config)


def test_legacy_app_config_constructor_builds_independent_catalog(openai_provider_config) -> None:
    config = AppConfig(provider=openai_provider_config)
    assert config.default_model == "legacy/default"
    assert resolve_model(config) == openai_provider_config
    openai_provider_config.model = "changed-snapshot"
    assert resolve_model(config).model != "changed-snapshot"


def test_environment_is_resolved_fresh_and_missing_variable_remains_literal(monkeypatch) -> None:
    monkeypatch.delenv("L_CODE_TEST_KEY", raising=False)
    config = load_config_data(_catalog_data())
    assert resolve_model(config).api_key == "${L_CODE_TEST_KEY}"
    monkeypatch.setenv("L_CODE_TEST_KEY", "new-key")
    assert resolve_model(config).api_key == "new-key"
    monkeypatch.setenv("L_CODE_TEST_KEY", "")
    with pytest.raises(ConfigError, match="api_key"):
        resolve_model(config)


@pytest.mark.parametrize("reference", ["", "deepseek", "deepseek/chat/extra", "missing/chat", "deepseek/missing"])
def test_catalog_rejects_invalid_default_reference(reference: str) -> None:
    raw = _catalog_data()
    raw["default_model"] = reference
    with pytest.raises(ConfigError):
        load_config_data(raw)


@pytest.mark.parametrize(("field", "value"), [
    ("protocol", "other"), ("base_url", ""), ("api_key", " "),
    ("timeout_seconds", True), ("timeout_seconds", float("inf")),
    ("context_window", False), ("context_window", 0),
    ("thinking", {"enabled": "false"}), ("thinking", {"budget_tokens": True}),
])
def test_invalid_nondefault_model_reports_qualified_path(field: str, value: object) -> None:
    raw = _catalog_data()
    raw["providers"]["deepseek"]["models"]["reasoner"][field] = value
    with pytest.raises(ConfigError, match=rf"providers.deepseek.models.reasoner.{field}"):
        load_config_data(raw)


def test_catalog_rejects_mixed_schema_and_invalid_ids() -> None:
    raw = _catalog_data()
    raw["provider"] = {}
    with pytest.raises(ConfigError, match="同时存在"):
        load_config_data(raw)
    raw = _catalog_data()
    raw["providers"]["bad/id"] = raw["providers"].pop("backup")
    with pytest.raises(ConfigError, match="ID"):
        load_config_data(raw)


def test_explicit_context_window_and_disabled_thinking_round_trip() -> None:
    raw = _catalog_data()
    raw["providers"]["deepseek"]["models"]["claude"].update({
        "context_window": 32000, "thinking": {"enabled": False},
    })
    config = load_config_data(serialize_config(load_config_data(raw)))
    resolved = resolve_model(config, "deepseek/claude")
    assert resolved.context_window == 32000
    assert resolved.thinking is not None and not resolved.thinking.enabled


def test_new_entry_id_is_stable_safe_and_unique() -> None:
    assert new_entry_id("DeepSeek", []) == "deepseek"
    assert new_entry_id("DeepSeek", ["deepseek", "deepseek-2"]) == "deepseek-3"
    assert new_entry_id("模型 A / B", []) == "模型-a-b"
    assert new_entry_id(" /// ", []) == "entry"


def _service(tmp_path: Path) -> SettingsService:
    return SettingsService(
        config_path=tmp_path / "lancher.yaml",
        global_mcp_path=tmp_path / "global-mcp.yaml",
        project_mcp_path=tmp_path / "project-mcp.yaml",
        permission_storage=PermissionStorage(
            project_rules_path=tmp_path / "project-permissions.yaml",
            user_rules_path=tmp_path / "user-permissions.yaml",
        ),
    )


def test_settings_migrates_legacy_with_exact_non_overwriting_backup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    legacy = b"# keep original comment\nprovider:\n  protocol: openai\n  model: old\n  base_url: https://old.test/v1\n  api_key: ${L_CODE_TEST_KEY}\n"
    service.config_path.write_bytes(legacy)
    snapshot = service.load()
    snapshot.config.providers["legacy"].models["default"].model_name = "updated"
    service.save(snapshot)
    backup = service.config_path.with_suffix(".yaml.bak")
    assert backup.read_bytes() == legacy
    assert "provider" not in yaml.safe_load(service.config_path.read_text(encoding="utf-8"))
    assert resolve_model(load_config(service.config_path)).model == "updated"
    service.config_path.write_bytes(legacy.replace(b"old\n", b"another\n"))
    service.save(service.load())
    assert backup.read_bytes() == legacy


def test_settings_rejects_invalid_catalog_before_any_write_or_backup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.config_path.write_text(yaml.safe_dump(_catalog_data()), encoding="utf-8")
    original = service.config_path.read_bytes()
    snapshot = service.load()
    snapshot.config.default_model = "missing/model"
    with pytest.raises(SettingsError, match="模型引用不存在"):
        service.save(snapshot)
    assert service.config_path.read_bytes() == original
    assert not service.config_path.with_suffix(".yaml.bak").exists()
    assert not service.global_mcp_path.exists()


def test_write_config_data_backs_up_legacy_and_keeps_existing_backup(tmp_path: Path) -> None:
    path = tmp_path / "lancher.yaml"
    old = b"# original\nprovider:\n  protocol: openai\n  model: old\n  base_url: https://old.test/v1\n  api_key: key\n"
    path.write_bytes(old)
    config = write_config_data(path, _catalog_data())
    assert path.with_suffix(".yaml.bak").read_bytes() == old
    assert resolve_model(load_config(path)) == resolve_model(config)
    path.write_bytes(old.replace(b"old\n", b"changed\n"))
    write_config(path, config)
    assert path.with_suffix(".yaml.bak").read_bytes() == old


def test_write_config_failure_keeps_original_file_and_reports_config_error(tmp_path: Path, monkeypatch) -> None:
    from lancher_code.config_system import writer

    path = tmp_path / "lancher.yaml"
    original = yaml.safe_dump(_catalog_data()).encode("utf-8")
    path.write_bytes(original)

    def fail_replace(*args) -> None:
        raise PermissionError("test denied")

    monkeypatch.setattr(writer.os, "replace", fail_replace)
    with pytest.raises(ConfigError, match="无法保存配置文件"):
        write_config(path, load_config_data(_catalog_data()))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_write_config_wraps_directory_creation_failure(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("occupied", encoding="utf-8")
    with pytest.raises(ConfigError, match="无法保存配置文件"):
        write_config(blocked / "lancher.yaml", load_config_data(_catalog_data()))
