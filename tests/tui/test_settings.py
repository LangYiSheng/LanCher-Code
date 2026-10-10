from pathlib import Path

import pytest
import yaml
from rich.text import Text
from textual.app import App
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, Tree

from lancher_code.config.loader import load_config
from lancher_code.permissions.models import PermissionRule
from lancher_code.providers.models import ModelDefinition
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.config.settings import SettingsService, SettingsError
from lancher_code.tui.settings.models import DeleteProviderScreen, ModelSettingsEditor
from lancher_code.tui.settings.confirmation import DiscardChangesScreen
from lancher_code.tui.settings.screen import SettingsScreen
from lancher_code.tui.settings.mcp import MCPSettingsEditor
from lancher_code.tui.settings.permissions import PermissionSettingsEditor


def _write_config(path: Path) -> None:
    path.write_text(
        """providers:
  test:
    name: 测试供应商
    protocol: openai
    base_url: https://example.test/v1
    api_key: secret
    models:
      default:
        model_name: gpt-test
default_model: test/default
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
    snapshot.config.providers["test"].models["default"].model_name = "gpt-updated"
    snapshot.global_mcp["demo"] = {"type": "http", "url": "https://example.test/mcp", "enabled": True}
    snapshot.project_rules.append(PermissionRule("RunCommand(git *)", "allow", "project", match_kind="glob"))

    service.save_models(snapshot.config)
    service.save_mcp("global", snapshot.global_mcp)
    service.save_rules("project", snapshot.project_rules)

    assert load_config(service.config_path).providers["test"].models["default"].model_name == "gpt-updated"
    assert yaml.safe_load(service.global_mcp_path.read_text(encoding="utf-8"))["mcp_servers"]["demo"]["type"] == "http"
    assert service.permission_storage.rules_for_scope("project")[0].match == "RunCommand(git *)"


def _settings_app(service: SettingsService, **kwargs) -> App:
    class SettingsApp(App):
        result = None

        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(service, **kwargs), self._receive)

        def _receive(self, result) -> None:
            self.result = result

    return SettingsApp()


def test_domain_saves_do_not_touch_other_files_and_preserve_latest_config(tmp_path) -> None:
    service = _service(tmp_path)
    original = service.config_path.read_bytes()
    snapshot = service.load()
    snapshot.config.providers["test"].models["default"].model_name = "changed"
    ui = service.load().config.ui
    ui.theme = "light"
    ui.busy_enter_action = "draft"
    service.save_ui(ui)
    service.save_models(snapshot.config)
    assert load_config(service.config_path).ui.theme == "light"
    assert load_config(service.config_path).ui.busy_enter_action == "draft"
    assert not service.global_mcp_path.exists()
    assert not service.project_mcp_path.exists()
    assert not service.permission_storage.project_rules_path.exists()
    assert not service.config_path.with_suffix(".yaml.bak").exists()
    before = service.config_path.read_bytes()
    service.save_mcp("project", {"demo": {"type": "http", "url": "https://example.test"}})
    assert service.config_path.read_bytes() == before
    assert not service.global_mcp_path.exists()
    service.save_rules("project", [PermissionRule("RunCommand(git status)", "allow", "project", match_kind="exact")])
    persisted = yaml.safe_load(service.permission_storage.project_rules_path.read_text(encoding="utf-8"))
    assert persisted["rules"][0]["match_kind"] == "exact"
    assert service.permission_storage.rules_for_scope("project")[0].match_kind == "exact"
    assert service.config_path.read_bytes() == before


def test_invalid_domain_save_does_not_write_or_make_migration_backup(tmp_path) -> None:
    service = _service(tmp_path)
    before = service.config_path.read_bytes()
    config = service.load().config
    config.providers["test"].models["default"].model_name = ""
    with pytest.raises(SettingsError):
        service.save_models(config)
    assert service.config_path.read_bytes() == before
    assert not service.config_path.with_suffix(".yaml.bak").exists()
    with pytest.raises(SettingsError):
        service.save_mcp("global", {"demo": {"type":"stdio", "command":"node", "args":"bad"}})
    assert not service.global_mcp_path.exists()


@pytest.mark.asyncio
async def test_catalog_exposes_current_and_default_independently(tmp_path) -> None:
    service = _service(tmp_path)
    config = service.load().config
    config.providers["test"].models["second"] = ModelDefinition(model_name="second", display_name="第二模型")
    service.save_models(config)
    selected, saved = [], []
    app = _settings_app(service, current_model_ref="test/default", on_model_selected=lambda ref: selected.append(ref), on_models_saved=lambda cfg: saved.append(cfg.default_model))
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        assert editor.kind is None
        assert not screen.query_one("#settings-save").display
        screen._picked("current", "test/second")
        assert selected == ["test/second"]
        assert screen.current_model_ref == "test/second"
        assert service.load().config.default_model == "test/default"
        screen._picked("default", "test/default")
        assert saved == ["test/default"]
        assert screen.current_model_ref == "test/second"
        assert "第二模型" in str(screen.query_one("#current-model-summary", Static).render())
        assert "gpt-test" in str(screen.query_one("#default-model-summary", Static).render())
        screen.action_close_settings()
        await pilot.pause()
        assert app.result.saved and app.result.runtime_applied


@pytest.mark.asyncio
async def test_entry_save_then_discard_only_current_draft(tmp_path) -> None:
    service = _service(tmp_path)
    callbacks = []
    app = _settings_app(service, on_models_saved=lambda config: callbacks.append(config))
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_model("test", "default")
        screen.query_one("#model-name", Input).value = "committed"
        screen.action_save_settings()
        await pilot.pause()
        await pilot.pause()
        assert editor.kind is None
        assert load_config(service.config_path).providers["test"].models["default"].model_name == "committed"
        assert len(callbacks) == 1
        editor.open_model("test", "default")
        screen.query_one("#model-name", Input).value = "discarded"
        screen.action_close_settings()
        await pilot.pause()
        assert isinstance(app.screen, DiscardChangesScreen)
        await pilot.click("#confirm-discard")
        await pilot.pause()
        assert editor.kind is None
        screen.action_close_settings()
        await pilot.pause()
        assert app.result.saved
        assert app.result.config.providers["test"].models["default"].model_name == "committed"
        assert load_config(service.config_path).providers["test"].models["default"].model_name == "committed"


@pytest.mark.asyncio
async def test_model_overrides_key_retention_and_restore_inheritance(tmp_path) -> None:
    service = _service(tmp_path)
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_model("test", "default")
        for field in ("protocol", "base-url", "api-key", "timeout"):
            screen.query_one(f"#inherit-{field}", Checkbox).value = False
        screen.query_one("#model-protocol", Select).value = "claude"
        screen.query_one("#model-api-key", Input).value = "${CUSTOM_KEY}"
        screen.query_one("#model-base-url", Input).value = "${CUSTOM_URL}"
        screen.query_one("#model-timeout", Input).value = "91"
        screen.query_one("#model-context-window", Input).value = "12345"
        screen.query_one("#model-thinking", Checkbox).value = True
        screen.query_one("#model-thinking-budget", Input).value = "2048"
        screen.action_save_settings()
        await pilot.pause()
        await pilot.pause()
        model = service.load().config.providers["test"].models["default"]
        assert (model.protocol, model.base_url, model.api_key, model.timeout_seconds) == ("claude", "${CUSTOM_URL}", "${CUSTOM_KEY}", 91)
        assert model.context_window == 12345 and model.thinking.budget_tokens == 2048
        editor.open_model("test", "default")
        assert screen.query_one("#model-api-key", Input).value == ""
        screen.action_save_settings()
        await pilot.pause()
        assert service.load().config.providers["test"].models["default"].api_key == "${CUSTOM_KEY}"
        editor.open_model("test", "default")
        for field in ("protocol", "base-url", "api-key", "timeout"):
            screen.query_one(f"#inherit-{field}", Checkbox).value = True
        await pilot.pause()
        assert not screen.query_one("#model-thinking-fields").display
        screen.action_save_settings()
        await pilot.pause()
        model = service.load().config.providers["test"].models["default"]
        assert model.protocol is None and model.base_url is None and model.api_key is None and model.timeout_seconds is None
        editor.open_provider("test")
        screen.query_one("#provider-name", Input).value = "[bold]供应商[/bold]"
        screen.action_save_settings()
        await pilot.pause()
        assert service.load().config.providers["test"].api_key == "secret"
        node = screen.query_one("#model-tree", Tree).root.children[0]
        assert "[bold]供应商[/bold]" in node.label.plain


@pytest.mark.asyncio
async def test_add_delete_entries_and_protect_default(tmp_path) -> None:
    service = _service(tmp_path)
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_model("test", "default")
        screen.delete_model_entry()
        assert "另一个新对话默认" in str(screen.query_one("#settings-error", Static).render())
        editor.open_provider()
        screen.query_one("#provider-name", Input).value = "同名供应商"
        screen.query_one("#provider-api-key", Input).value = "sample-key"
        screen.action_save_settings()
        await pilot.pause()
        provider_id = next(key for key in editor.config.providers if key != "test")
        editor.open_model(provider_id)
        screen.query_one("#model-name", Input).value = "same-api-model"
        screen.action_save_settings()
        await pilot.pause()
        old_id = next(iter(editor.config.providers[provider_id].models))
        editor.open_model(provider_id, old_id)
        screen.delete_model_entry()
        editor.open_model(provider_id)
        screen.query_one("#model-name", Input).value = "same-api-model"
        screen.action_save_settings()
        await pilot.pause()
        new_id = next(iter(editor.config.providers[provider_id].models))
        assert old_id != new_id
        editor.open_provider(provider_id)
        screen.delete_model_entry()
        await pilot.pause()
        assert isinstance(app.screen, DeleteProviderScreen)
        await pilot.click("#delete-provider-confirm")
        await pilot.pause()
        assert provider_id not in service.load().config.providers


@pytest.mark.asyncio
async def test_mcp_save_pending_cumulative_and_no_implicit_other_domain_save(tmp_path) -> None:
    service = _service(tmp_path)
    original = service.config_path.read_bytes()
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen._show_tab("mcp")
        screen.query_one(MCPSettingsEditor).edit_mcp()
        screen.query_one("#mcp-name", Input).value = "demo"
        screen.query_one("#mcp-type", Select).value = "http"
        screen.query_one("#mcp-target", Input).value = "https://example.test/mcp"
        screen.action_save_settings()
        await pilot.pause()
        assert service.config_path.read_bytes() == original
        assert not service.project_mcp_path.exists()
        screen._show_tab("model")
        screen.query_one(ModelSettingsEditor).open_model("test", "default")
        screen.query_one("#model-name", Input).value = "updated"
        screen.action_save_settings()
        await pilot.pause()
        screen.action_close_settings()
        await pilot.pause()
        assert app.result.saved and app.result.mcp_pending and not app.result.restart_required


@pytest.mark.asyncio
async def test_permission_editor_preserves_matching_semantics_and_order(tmp_path) -> None:
    service = _service(tmp_path)
    service.save_rules("project", [PermissionRule("RunCommand(git *)", "allow", "project", match_kind="glob"), PermissionRule("RunCommand(git status)", "deny", "project", match_kind="exact")])
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen._show_tab("permissions")
        screen.query_one(PermissionSettingsEditor).edit_rule(1)
        assert screen.query_one("#rule-match-kind", Select).value == "exact"
        screen.query_one("#rule-result", Select).value = "allow"
        screen.action_save_settings()
        await pilot.pause()
        assert service.permission_storage.rules_for_scope("project")[1].match_kind == "exact"
        screen.query_one(PermissionSettingsEditor).edit_rule(1)
        screen.query_one(PermissionSettingsEditor).rule_action(Button.Pressed(screen.query_one("#rule-up", Button)))
        assert service.permission_storage.rules_for_scope("project")[0].match_kind == "exact"
        screen.query_one(PermissionSettingsEditor).edit_rule()
        assert screen.query_one("#rule-match-kind", Select).value == "exact"
        assert not service.permission_storage.user_rules_path.exists()


@pytest.mark.asyncio
async def test_ui_preferences_apply_only_on_save_and_survive_model_save(tmp_path) -> None:
    service = _service(tmp_path)
    callbacks = []
    app = _settings_app(service, on_ui_saved=lambda ui: callbacks.append(ui))
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen._show_tab("ui")
        screen.query_one("#ui-theme", Select).value = "light"
        screen.query_one("#ui-busy-enter", Select).value = "steer"
        assert app.theme == "lancher-dark"
        screen.action_save_settings()
        await pilot.pause()
        assert app.theme == "lancher-light"
        assert len(callbacks) == 1 and callbacks[0].busy_enter_action == "steer"
        screen.query_one(ModelSettingsEditor).open_model("test", "default")
        screen.query_one("#model-name", Input).value = "renamed"
        screen.action_save_settings()
        await pilot.pause()
        assert service.load().config.ui.theme == "light"
        assert service.load().config.ui.busy_enter_action == "steer"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(110, 42), (60, 28), (40, 24), (32, 16)])
async def test_catalog_and_entry_actions_remain_reachable(tmp_path, size) -> None:
    app = _settings_app(_service(tmp_path))
    async with app.run_test(size=size) as pilot:
        screen = app.screen
        for selector in ("#model-tree", "#provider-new"):
            widget = screen.query_one(selector)
            widget.scroll_visible(animate=False)
            await pilot.pause()
            assert widget.region.x >= 0 and widget.region.right <= size[0]
            if selector == "#provider-new":
                assert widget.region.y >= 0 and widget.region.bottom <= size[1]
        screen.query_one(ModelSettingsEditor).open_model("test", "default")
        await pilot.pause()
        for selector in ("#model-name", "#model-display-name", "#settings-save", "#settings-cancel"):
            widget = screen.query_one(selector)
            widget.scroll_visible(animate=False)
            await pilot.pause()
            assert widget.region.x >= 0 and widget.region.right <= size[0]
            assert widget.region.y >= 0 and widget.region.bottom <= size[1]


@pytest.mark.asyncio
async def test_saved_config_survives_runtime_callback_failure(tmp_path) -> None:
    service = _service(tmp_path)
    def fail(config):
        raise RuntimeError("runtime busy")
    app = _settings_app(service, on_models_saved=fail, on_model_selected=fail)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_model("test", "default")
        screen.query_one("#model-name", Input).value = "saved-before-callback"
        screen.action_save_settings()
        await pilot.pause()
        assert load_config(service.config_path).providers["test"].models["default"].model_name == "saved-before-callback"
        assert editor.kind is None
        assert "配置已保存" in str(screen.query_one("#settings-error", Static).render())
        screen._picked("current", "missing/model")
        assert screen.current_model_ref == "test/default"
        screen.action_close_settings()
        await pilot.pause()
        assert app.result.saved and not app.result.runtime_applied


@pytest.mark.asyncio
async def test_mcp_duplicate_name_rejected_and_http_ignores_hidden_stdio_args(tmp_path) -> None:
    service = _service(tmp_path)
    service.save_mcp("global", {"first":{"type":"http", "url":"https://one.test"}, "second":{"type":"http", "url":"https://two.test", "extra":42}})
    app = _settings_app(service)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen._show_tab("mcp")
        screen.query_one(MCPSettingsEditor).edit_mcp("second")
        screen.query_one("#mcp-name", Input).value = "first"
        screen.action_save_settings()
        await pilot.pause()
        assert "同名" in str(screen.query_one("#settings-error", Static).render())
        assert len(service.load().global_mcp) == 2
        screen.query_one("#mcp-name", Input).value = "renamed"
        screen.query_one("#mcp-args", Input).value = "[invalid yaml"
        screen.action_save_settings()
        await pilot.pause()
        assert service.load().global_mcp["renamed"]["extra"] == 42
        assert "second" not in service.load().global_mcp


@pytest.mark.asyncio
async def test_narrow_keyboard_save_discard_and_mcp_pending_status_survive_reopen(tmp_path) -> None:
    service = _service(tmp_path)
    service.save_mcp("global", {"demo":{"type":"http", "url":"https://example.test"}})
    app = _settings_app(service)
    async with app.run_test(size=(32, 16)) as pilot:
        screen = app.screen
        assert screen._mcp_pending
        assert "/mcp reload" in str(screen.query_one("#settings-restart", Static).render())
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_model("test", "default")
        screen.query_one("#model-name", Input).value = "keyboard-save"
        await pilot.press("ctrl+s")
        assert load_config(service.config_path).providers["test"].models["default"].model_name == "keyboard-save"
        editor.open_model("test", "default")
        screen.query_one("#model-name", Input).value = "draft-only"
        await pilot.press("escape")
        assert isinstance(app.screen, DiscardChangesScreen)
        for selector in ("#keep-editing", "#confirm-discard"):
            button = app.screen.query_one(selector, Button)
            assert button.region.x >= 0 and button.region.right <= 32
            assert button.region.y >= 0 and button.region.bottom <= 16
        await pilot.press("escape")
        assert app.screen is screen and editor.dirty
        await pilot.press("escape")
        await pilot.click("#confirm-discard")
        await pilot.pause()
        assert editor.kind is None
        await pilot.press("escape")
        assert app.result.saved and app.result.mcp_pending and not app.result.restart_required
        assert load_config(service.config_path).providers["test"].models["default"].model_name == "keyboard-save"
    app = _settings_app(service)
    async with app.run_test(size=(32, 16)) as pilot:
        assert "/mcp reload" in str(app.screen.query_one("#settings-restart", Static).render())
        await pilot.press("escape")
        assert app.result.mcp_pending and not app.result.restart_required


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(32, 16), (100, 40)])
async def test_initial_catalog_uses_content_area_and_shows_provider_and_model(tmp_path, size) -> None:
    app = _settings_app(_service(tmp_path))
    async with app.run_test(size=size) as pilot:
        screen = app.screen
        pages = screen.query_one("#settings-pages")
        tree = screen.query_one("#model-tree", Tree)
        current = screen.query_one("#current-model-summary")
        tabs = screen.query_one("#settings-tabs")
        assert all(not screen.query_one(f"#settings-{name}").display for name in ("error", "notice", "restart"))
        assert current.region.y - tabs.region.bottom <= 1
        assert pages.scroll_y == 0 and tree.scroll_y == 0
        # 首屏不仅有控件框：供应商行及其第一条模型均在内容裁剪区之内。
        assert tree.root.children and tree.root.children[0].children
        assert tree.content_region.y >= pages.content_region.y
        assert tree.content_region.y + 2 <= pages.content_region.bottom
        assert screen.query_one("#pick-current").region.height == 1
        assert screen.query_one("#pick-default").region.height == 1
        if size[0] == 32:
            assert str(screen.query_one("#tab-ui", Button).label) == "外观"
        screen._show_error("校验错误")
        await pilot.pause()
        assert screen.query_one("#settings-error").display
        screen._show_error("")
        screen._notice("保存完成")
        await pilot.pause()
        assert not screen.query_one("#settings-error").display
        assert screen.query_one("#settings-notice").display
        screen._show_tab("model")
        await pilot.pause()
        assert not screen.query_one("#settings-notice").display
        assert tree.content_region.y + 2 <= pages.content_region.bottom
