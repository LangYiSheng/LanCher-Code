from pathlib import Path

import pytest
import yaml
from textual.app import App
from textual.widgets import Button, Input, Static

from lancher_code.config_system.loader import load_config
from lancher_code.models import PermissionRule
from lancher_code.permission_engine import PermissionStorage
from lancher_code.settings_service import SettingsService
from lancher_code.tui_views.settings import SettingsScreen


def _write_config(path: Path) -> None:
    path.write_text(
        """provider:
  protocol: openai
  model: gpt-test
  base_url: https://example.test/v1
  api_key: secret
runtime:
  permission_mode: default
""",
        encoding="utf-8",
    )


def _service(tmp_path: Path) -> SettingsService:
    config = tmp_path / "home" / ".lancher" / "lancher.yaml"
    config.parent.mkdir(parents=True)
    _write_config(config)
    project = tmp_path / "project" / ".lancher"
    return SettingsService(
        config_path=config,
        global_mcp_path=config.parent / "mcp.yaml",
        project_mcp_path=project / "mcp.yaml",
        permission_storage=PermissionStorage(
            project_rules_path=project / "permissions.yaml",
            user_rules_path=config.parent / "permissions.yaml",
        ),
    )


def test_settings_service_saves_layers_and_hot_replaces_permissions(tmp_path: Path) -> None:
    service = _service(tmp_path)
    snapshot = service.load()
    snapshot.config.provider.model = "gpt-updated"
    snapshot.global_mcp["demo"] = {"type": "http", "url": "https://example.test/mcp", "enabled": True}
    snapshot.project_rules.append(PermissionRule("Bash(git *)", "allow", "project"))

    service.save(snapshot)

    assert load_config(service.config_path).provider.model == "gpt-updated"
    assert yaml.safe_load(service.global_mcp_path.read_text(encoding="utf-8"))["mcp_servers"]["demo"]["type"] == "http"
    assert service.permission_storage.rules_for_scope("project")[0].match == "Bash(git *)"


@pytest.mark.asyncio
async def test_settings_screen_switches_tabs_and_preserves_masked_api_key(tmp_path: Path) -> None:
    service = _service(tmp_path)

    class TestApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(service))

    app = TestApp()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen.query_one("#page-model").has_class("-active")
        assert screen.query_one("#tab-model", Button).region.height == 1
        assert screen.query_one("#tab-model", Button).label.plain == "模型设置"
        assert screen.query_one("#model-name", Input).styles.background.a == 1
        assert screen.query_one("#model-name", Input).region.height == 3
        select_label = screen.query_one("#model-protocol Static#label", Static)
        assert "OpenAI" in str(select_label.render())
        save_button = screen.query_one("#settings-save", Button)
        actions = screen.query_one("#settings-actions")
        assert save_button.region.height == 3
        assert save_button.region.bottom <= actions.region.bottom
        await pilot.press("right")
        assert screen.query_one("#page-mcp").has_class("-active")
        assert screen.query_one("#model-api-key").value == ""
