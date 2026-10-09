from pathlib import Path

import pytest
import yaml
from rich.text import Text
from textual.app import App
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from lancher_code.config_system.loader import load_config
from lancher_code.models import PermissionRule
from lancher_code.permission_engine import PermissionStorage
from lancher_code.settings_service import SettingsService
from lancher_code.tui_views.model_settings import DeleteProviderScreen, ModelSettingsEditor
from lancher_code.tui_views.settings import DiscardChangesScreen, SettingsScreen


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
    snapshot.config.providers["legacy"].models["default"].model_name = "gpt-updated"
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
        assert screen.query_one("#tab-model", Button).content_region.height == 1
        assert screen.query_one("#tab-model", Button).has_class("-selected")
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


def _settings_app(service: SettingsService) -> App:
    class SettingsApp(App):
        result = None

        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(service), self._receive)

        def _receive(self, result) -> None:
            self.result = result

    return SettingsApp()


@pytest.mark.asyncio
async def test_settings_catalog_crud_keeps_ids_and_protects_default(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = _settings_app(service)
    async with app.run_test(size=(110, 42)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.delete_model()
        assert "先选择另一个默认模型" in str(screen.query_one("#settings-error", Static).render())
        editor.new_provider()
        provider_id = editor.provider_id
        screen.query_one("#provider-name", Input).value = "DeepSeek"
        screen.query_one("#provider-base-url", Input).value = "https://example.test/deepseek"
        screen.query_one("#provider-api-key", Input).value = "second-secret"
        editor.new_model()
        model_id = editor.model_id
        screen.query_one("#model-name", Input).value = "deepseek-chat"
        screen.query_one("#model-display-name", Input).value = "编程助手"
        editor.apply_entry()
        reference = f"{provider_id}/{model_id}"
        screen.query_one("#default-model", Select).value = reference
        await pilot.pause()
        screen.query_one("#provider-name", Input).value = "改名后的供应商"
        screen.query_one("#model-display-name", Input).value = "日常编程"
        editor.apply_entry()
        assert editor.provider_id == provider_id
        assert editor.model_id == model_id
        assert editor.config.default_model == reference
        editor.delete_provider()
        assert "其他供应商" in str(screen.query_one("#settings-error", Static).render())
        screen.query_one("#default-model", Select).value = "legacy/default"
        await pilot.pause()
        editor.delete_provider()
        await pilot.pause()
        assert isinstance(app.screen, DeleteProviderScreen)
        assert any("日常编程" in str(widget.render()) for widget in app.screen.query(Static))
        await pilot.click("#delete-provider-confirm")
        await pilot.pause()
        assert provider_id not in editor.config.providers
        assert service.load().config.default_model == "legacy/default"


@pytest.mark.asyncio
async def test_settings_model_overrides_and_api_key_retention(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = _settings_app(service)
    async with app.run_test(size=(110, 42)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        for name in ("protocol", "base-url", "api-key", "timeout"):
            assert screen.query_one(f"#inherit-{name}", Checkbox).value is True
        screen.query_one("#inherit-api-key", Checkbox).value = False
        screen.query_one("#inherit-base-url", Checkbox).value = False
        screen.query_one("#model-api-key", Input).value = "${CUSTOM_KEY}"
        screen.query_one("#model-base-url", Input).value = "https://override.test/v1"
        editor.apply_entry()
        model = editor.config.providers["legacy"].models["default"]
        assert model.api_key == "${CUSTOM_KEY}"
        assert model.base_url == "https://override.test/v1"
        assert model.protocol is None
        assert screen.query_one("#model-api-key", Input).value == ""
        editor.apply_entry()
        assert editor.config.providers["legacy"].models["default"].api_key == "${CUSTOM_KEY}"
        screen.query_one("#inherit-api-key", Checkbox).value = True
        screen.query_one("#provider-base-url", Input).value = "https://provider-new.test/v1"
        await pilot.pause()
        editor.apply_entry()
        assert editor.config.providers["legacy"].models["default"].api_key is None
        screen.action_save_settings()
        await pilot.pause()
        assert app.result.saved is True
        assert app.result.restart_required is False
        assert app.result.config.providers["legacy"].api_key == "secret"
        assert load_config(service.config_path).provider.base_url == "https://override.test/v1"


@pytest.mark.asyncio
async def test_settings_catalog_displays_bracketed_names_as_literal_text(tmp_path: Path) -> None:
    app = _settings_app(_service(tmp_path))
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen.query_one("#provider-name", Input).value = "[bold]供应商[/bold]"
        screen.query_one("#model-display-name", Input).value = "[fast][bold]助手[/bold]"
        screen.query_one(ModelSettingsEditor).apply_entry()
        await pilot.pause()
        provider_label = screen.query_one("#providers-table", DataTable).get_row("legacy")[0]
        model_label = screen.query_one("#models-table", DataTable).get_row("default")[0]
        assert isinstance(provider_label, Text)
        assert provider_label.plain == "[bold]供应商[/bold]"
        assert isinstance(model_label, Text)
        assert model_label.plain == "★ [fast][bold]助手[/bold]"
        selected_label = screen.query_one("#default-model Static#label", Static)
        assert "[fast][bold]助手[/bold]" in str(selected_label.render())


@pytest.mark.asyncio
async def test_settings_cancel_does_not_write_and_mcp_change_requests_restart(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = service.config_path.read_bytes()
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen.query_one("#model-name", Input).value = "changed"
        screen.action_close_settings()
        await pilot.pause()
        assert isinstance(app.screen, DiscardChangesScreen)
        await pilot.click("#confirm-discard")
        assert service.config_path.read_bytes() == before
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen.snapshot.global_mcp["demo"] = {"type": "http", "url": "https://example.test/mcp"}
        screen.action_save_settings()
        await pilot.pause()
        assert app.result.restart_required is True


@pytest.mark.asyncio
async def test_settings_incomplete_entries_can_be_left_and_cancelled(tmp_path: Path) -> None:
    service = _service(tmp_path)
    before = service.config_path.read_bytes()
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.new_provider()
        editor.new_model()
        created_id = editor.provider_id
        table = screen.query_one("#providers-table", DataTable)
        table.move_cursor(row=0)
        table.focus()
        await pilot.press("enter")
        assert editor.provider_id == "legacy"
        assert created_id in editor.config.providers
        screen.action_save_settings()
        assert app.screen is screen
        assert "api_key" in str(screen.query_one("#settings-error", Static).render()) or "model_name" in str(screen.query_one("#settings-error", Static).render())
        assert service.config_path.read_bytes() == before
        screen.action_close_settings()
        await pilot.pause()
        await pilot.click("#confirm-discard")
        assert service.config_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_kind", ["provider", "model"])
@pytest.mark.parametrize("reopen", [False, True])
async def test_recreated_catalog_entries_never_reuse_deleted_session_references(
    tmp_path: Path, entry_kind: str, reopen: bool,
) -> None:
    service = _service(tmp_path)

    def create_entry(screen: SettingsScreen) -> tuple[str, str]:
        editor = screen.query_one(ModelSettingsEditor)
        if entry_kind == "provider":
            editor.new_provider()
            screen.query_one("#provider-name", Input).value = "同名供应商"
            screen.query_one("#provider-api-key", Input).value = "sample-secret"
        editor.new_model()
        screen.query_one("#model-name", Input).value = "same-api-model"
        editor.apply_entry()
        return editor.provider_id, f"{editor.provider_id}/{editor.model_id}"

    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        old_provider_id, old_ref = create_entry(screen)
        editor = screen.query_one(ModelSettingsEditor)
        if entry_kind == "provider":
            editor.delete_provider()
            await pilot.pause()
            await pilot.click("#delete-provider-confirm")
            await pilot.pause()
        else:
            editor.delete_model()
        if reopen:
            screen.action_save_settings()
            await pilot.pause()
            assert app.result.saved is True
        else:
            new_provider_id, new_ref = create_entry(screen)
            assert new_ref != old_ref
            if entry_kind == "provider":
                assert new_provider_id != old_provider_id

    if reopen:
        app = _settings_app(service)
        async with app.run_test(size=(100, 40)):
            new_provider_id, new_ref = create_entry(app.screen)
            assert new_ref != old_ref
            if entry_kind == "provider":
                assert new_provider_id != old_provider_id


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(110, 42), (60, 28), (40, 24), (32, 16)])
async def test_settings_catalog_layout_and_scroll_reachability(tmp_path: Path, size) -> None:
    app = _settings_app(_service(tmp_path))
    async with app.run_test(size=size) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        assert editor.has_class("-narrow") == (size[0] < 75)
        for selector in ("#providers-table", "#models-table", "#model-name", "#catalog-apply"):
            widget = screen.query_one(selector)
            widget.scroll_visible(animate=False)
            await pilot.pause()
            assert widget.region.x >= 0
            assert widget.region.right <= size[0]
            assert widget.region.y >= 0
            assert widget.region.bottom <= size[1]
        save_button = screen.query_one("#settings-save", Button)
        assert save_button.region.bottom <= size[1]
