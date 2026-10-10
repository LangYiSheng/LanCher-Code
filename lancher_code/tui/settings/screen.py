from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Awaitable, Callable

import yaml
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Static

from lancher_code.providers.catalog import iter_model_refs
from lancher_code.config.models import AppConfig, UIConfig
from lancher_code.config.settings import SettingsError, SettingsService, SettingsSnapshot
from lancher_code.tui.model_picker import ModelPickerScreen
from lancher_code.tui.settings.models import DeleteProviderScreen, ModelSettingsEditor
from lancher_code.tui.theme import apply_theme
from lancher_code.tui.settings.confirmation import DiscardChangesScreen
from lancher_code.tui.settings.common import SettingsDomainEditor, EditorViewChanged, DomainSaved, DomainError
from lancher_code.tui.settings.mcp import MCPSettingsEditor
from lancher_code.tui.settings.permissions import PermissionSettingsEditor
from lancher_code.tui.settings.appearance import AppearanceSettingsEditor

from lancher_code.tui.settings.styles import SETTINGS_CSS

TAB_IDS = ("model", "mcp", "permissions", "ui")


@dataclass(frozen=True, slots=True)
class SettingsResult:
    saved: bool
    restart_required: bool = False
    config: AppConfig | None = None
    runtime_applied: bool = False
    mcp_pending: bool = False


class SettingsScreen(Screen[SettingsResult]):
    """逐条提交设置；已保存状态不会被后来取消的草稿覆盖。"""

    BINDINGS = [Binding("escape", "close_settings", "返回", show=False), Binding("ctrl+s", "save_settings", "保存", show=False)]
    CSS = SETTINGS_CSS

    def __init__(self, service: SettingsService, current_model_ref: str | None = None,
                 on_models_saved: Callable[[AppConfig], str | None] | None = None,
                 on_model_selected: Callable[[str], str | None] | None = None,
                 on_ui_saved: Callable[[UIConfig], None] | None = None,
                 on_mcp_saved: Callable[[], Awaitable[str]] | None = None) -> None:
        super().__init__()
        self.service = service
        self.current_model_ref = current_model_ref
        self._on_models_saved, self._on_model_selected, self._on_ui_saved = on_models_saved, on_model_selected, on_ui_saved
        self._on_mcp_saved = on_mcp_saved
        self.snapshot: SettingsSnapshot | None = None
        self._active_tab = "model"
        self._saved = False
        self._mcp_pending = service.mcp_restart_required if on_mcp_saved is None else False
        self._runtime_applied = False

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-root"):
            yield Static("❯ LanCher Code / 设置 / 模型", id="settings-title")
            with Horizontal(id="settings-tabs"):
                for tab_id, label in zip(TAB_IDS, ("模型", "MCP 工具", "权限规则", "外观与输入")):
                    yield Button(label, id=f"tab-{tab_id}", classes="settings-tab")
            yield Static("", id="settings-restart", markup=False)
            yield Static("", id="settings-error", markup=False)
            yield Static("", id="settings-notice", markup=False)
            with VerticalScroll(id="settings-pages"):
                yield ModelSettingsEditor(self._show_error)
                yield MCPSettingsEditor(self.service)
                yield PermissionSettingsEditor(self.service)
                yield AppearanceSettingsEditor(self.service)
            with Horizontal(id="settings-actions"):
                yield Button("‹ 返回对话", id="settings-cancel")
                yield Button("保存", id="settings-save")
            yield Static("Enter 打开 · Esc 返回 · Ctrl+S 保存当前编辑", id="settings-help")


    def on_mount(self) -> None:
        try:
            self.snapshot = self.service.load()
            self.current_model_ref = self.current_model_ref or self.snapshot.config.default_model
            apply_theme(self.app, self.snapshot.config.ui.theme)
            self.query_one(ModelSettingsEditor).load_config(self.snapshot.config, self.current_model_ref)
            for editor in self.query(SettingsDomainEditor):
                editor.load(self.snapshot)
            self._show_tab("model")
        except SettingsError as exc:
            self._show_error(str(exc))
        self._refresh_responsive_layout()

    def on_resize(self) -> None:
        self._refresh_responsive_layout()

    def _refresh_responsive_layout(self) -> None:
        compact = self.size.width < 60
        self.query_one("#settings-root").set_class(compact, "-narrow")
        self._refresh_tab_labels()
        self.query_one(ModelSettingsEditor).set_compact(compact)

    def _refresh_tab_labels(self) -> None:
        compact = self.size.width < 60
        labels = ("模型", "MCP", "权限", "外观") if compact else ("模型", "MCP 工具", "权限规则", "外观与输入")
        for tab_id, label in zip(TAB_IDS, labels):
            self.query_one(f"#tab-{tab_id}", Button).label = f"› {label}" if not compact and tab_id == self._active_tab else label

    @property
    def _editing(self) -> bool:
        return self.query_one(ModelSettingsEditor).editing if self._active_tab == "model" else self._domain_editor().editing

    def _dirty(self) -> bool:
        if self._active_tab == "model":
            return self.query_one(ModelSettingsEditor).dirty
        return self._domain_editor().dirty


    def _guard(self, action: Callable[[], None]) -> None:
        if self._dirty():
            self.app.push_screen(DiscardChangesScreen(), lambda discard: action() if discard else None)
        else:
            action()

    @on(Button.Pressed, ".settings-tab")
    def choose_tab(self, event: Button.Pressed) -> None:
        tab_id = event.button.id.removeprefix("tab-")
        self._guard(lambda: self._show_tab(tab_id))

    def _show_tab(self, tab_id: str) -> None:
        if self.snapshot is None:
            return
        self._active_tab = tab_id
        self._show_error("")
        self.query_one("#settings-notice", Static).update("")
        self.query_one("#settings-notice").display = False
        self.query_one(ModelSettingsEditor).show_catalog()
        for candidate in TAB_IDS:
            self.query_one(f"#page-{candidate}").set_class(candidate == tab_id, "-active")
            self.query_one(f"#tab-{candidate}").set_class(candidate == tab_id, "-selected")
        self._refresh_tab_labels()
        for editor in self.query(SettingsDomainEditor):
            editor.show_catalog()
        self._refresh_actions()
        self.query_one("#settings-pages", VerticalScroll).scroll_home(animate=False)

    @on(ModelSettingsEditor.ViewChanged)
    def model_view_changed(self) -> None:
        self._refresh_actions()
        self.query_one("#settings-pages", VerticalScroll).scroll_home(animate=False)

    def _refresh_actions(self) -> None:
        self.query_one("#settings-restart", Static).update("MCP 有已保存的更改 · /mcp reload 应用" if self._mcp_pending else "")
        self.query_one("#settings-restart").display = self._mcp_pending
        editing = self._editing
        self.query_one("#settings-save").display = editing
        self.query_one("#settings-cancel", Button).label = "‹ 返回目录" if editing else "‹ 返回对话"
        labels = {"model":"模型", "mcp":"MCP 工具", "permissions":"权限规则", "ui":"外观与输入"}
        editor = self.query_one(ModelSettingsEditor)
        suffix = " / 供应商连接" if self._active_tab == "model" and editor.kind == "provider" else " / 编辑" if editing else ""
        self.query_one("#settings-title", Static).update(f"❯ LanCher Code / 设置 / {labels[self._active_tab]}{suffix}")
        save_label = "保存供应商" if editor.kind == "provider" else "保存模型"
        self.query_one("#settings-save", Button).label = save_label if self._active_tab == "model" else {"mcp":"保存服务器", "permissions":"保存规则", "ui":"保存偏好"}[self._active_tab]

    @on(ModelSettingsEditor.PickRequested)
    def pick_requested(self, event: ModelSettingsEditor.PickRequested) -> None:
        editor = self.query_one(ModelSettingsEditor)
        if event.purpose == "add-model":
            provider_id = editor.provider_id
            self._guard(lambda: editor.open_model(provider_id))
        else:
            purpose = event.purpose
            self.app.push_screen(ModelPickerScreen(self.snapshot.config, self.current_model_ref, purpose=purpose), lambda ref: self._picked(purpose, ref))

    def _picked(self, purpose: str, reference: str | None) -> None:
        if reference is None:
            return
        if purpose == "default":
            candidate = deepcopy(self.snapshot.config)
            candidate.default_model = reference
            self._commit_models(candidate, "新对话默认已保存，本次模型不变。")
        else:
            try:
                if self._on_model_selected is not None:
                    actual = self._on_model_selected(reference)
                    reference = actual if isinstance(actual, str) else reference
                self.current_model_ref = reference
                self.query_one(ModelSettingsEditor).load_config(self.snapshot.config, reference)
                self._notice("已切换本次模型，新对话默认不变。")
            except Exception as exc:
                self._show_error(f"本次模型切换失败：{exc}")

    def _commit_models(self, config: AppConfig, notice: str) -> None:
        try:
            committed = self.service.save_models(config)
        except (SettingsError, ValueError) as exc:
            self._show_error(str(exc))
            for selector in ("#provider-advanced", "#model-advanced"):
                self.query_one(selector, Collapsible).collapsed = False
            return
        self.snapshot.config = committed
        self._saved = True
        callback_error = None
        try:
            if self._on_models_saved:
                actual = self._on_models_saved(committed)
                if isinstance(actual, str):
                    self.current_model_ref = actual
                self._runtime_applied = True
            if self.current_model_ref not in iter_model_refs(committed.providers):
                self.current_model_ref = committed.default_model
        except Exception as exc:
            self._runtime_applied = False
            callback_error = f"配置已保存，本次模型更新失败：{exc}"
        self.query_one(ModelSettingsEditor).load_config(committed, self.current_model_ref)
        self._refresh_actions()
        self._notice(notice)
        if callback_error:
            self._show_error(callback_error)

    @on(ModelSettingsEditor.DeleteRequested)
    def delete_model_entry(self) -> None:
        editor = self.query_one(ModelSettingsEditor)
        def remove() -> None:
            candidate = deepcopy(self.snapshot.config)
            if editor.kind == "model":
                ref = f"{editor.provider_id}/{editor.model_id}"
                if ref == candidate.default_model:
                    self._show_error("请先选择另一个新对话默认模型，再删除此模型。"); return
                candidate.providers[editor.provider_id].models.pop(editor.model_id)
                self._commit_models(candidate, "模型已删除。")
            else:
                if candidate.default_model.startswith(f"{editor.provider_id}/"):
                    self._show_error("请先选择其他供应商的新对话默认模型，再删除此供应商。"); return
                provider_id = editor.provider_id
                provider = candidate.providers[provider_id]
                labels = [m.display_name or m.model_name for m in provider.models.values()]
                def confirmed(yes: bool | None) -> None:
                    if yes:
                        candidate.providers.pop(provider_id)
                        self._commit_models(candidate, "供应商及其模型已删除。")
                self.app.push_screen(DeleteProviderScreen(provider.name, labels), confirmed)
        self._guard(remove)


    @on(Button.Pressed, "#settings-save")
    def save_pressed(self) -> None:
        self.action_save_settings()

    def _save_with_error(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except (SettingsError, ValueError, yaml.YAMLError) as exc:
            self._show_error(str(exc))
            for selector in ("#provider-advanced", "#model-advanced", "#mcp-advanced"):
                self.query_one(selector, Collapsible).collapsed = False

    def action_save_settings(self) -> None:
        if self.snapshot is None or not self._editing: return
        def save() -> None:
            if self._active_tab == "model":
                self._commit_models(self.query_one(ModelSettingsEditor).collect(), "已保存；本次模型与新对话默认分别管理。")
            else:
                self._domain_editor().save()
        self._save_with_error(save)

    def _domain_editor(self) -> SettingsDomainEditor:
        return self.query_one(f"#page-{self._active_tab}", SettingsDomainEditor)

    @on(EditorViewChanged)
    def domain_view_changed(self) -> None:
        self._refresh_actions()
        self.query_one("#settings-pages", VerticalScroll).scroll_home(animate=False)

    @on(DomainError)
    def domain_error(self, event: DomainError) -> None:
        self._show_error(event.message)

    @on(DomainSaved)
    async def domain_saved(self, event: DomainSaved) -> None:
        self._saved = True
        self._mcp_pending |= event.domain == "mcp" and event.restart_required
        callback_error = None
        notice = event.notice
        if event.domain == "mcp" and event.restart_required and self._on_mcp_saved is not None:
            self._notice("MCP 已保存 · 正在应用…")
            try:
                notice = await self._on_mcp_saved()
            except Exception as exc:
                callback_error = f"MCP 已保存，应用失败：{exc}；可运行 /mcp reload 重试。"
            else:
                self._mcp_pending = False
        if event.ui is not None:
            apply_theme(self.app, event.ui.theme)
            try:
                if self._on_ui_saved:
                    self._on_ui_saved(deepcopy(event.ui))
            except Exception as exc:
                callback_error = f"偏好已保存，当前界面更新失败：{exc}"
        self._show_tab("model" if event.domain == "ui" else event.domain)
        self._notice(notice)
        if callback_error:
            self._show_error(callback_error)

    @on(Button.Pressed, "#settings-cancel")
    def cancel_pressed(self) -> None:
        self.action_close_settings()

    def action_close_settings(self) -> None:
        def leave() -> None:
            if self._editing:
                self._show_tab(self._active_tab if self._active_tab != "ui" else "model")
            else:
                self.dismiss(SettingsResult(self._saved, False, deepcopy(self.snapshot.config) if self.snapshot else None,
                                            self._runtime_applied, self._mcp_pending))
        self._guard(leave)

    def _show_error(self, message: str) -> None:
        self.query_one("#settings-error", Static).update(message)
        self.query_one("#settings-error").display = bool(message)

    def _notice(self, message: str) -> None:
        self._show_error("")
        self.query_one("#settings-notice", Static).update(message)
        self.query_one("#settings-notice").display = bool(message)
