from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

import yaml
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Input, Select, Static

from lancher_code.model_catalog import iter_model_refs
from lancher_code.models import AppConfig, PermissionRule, UIConfig
from lancher_code.settings_service import SettingsError, SettingsService, SettingsSnapshot
from lancher_code.tui_views.model_picker import ModelPickerScreen
from lancher_code.tui_views.model_settings import DeleteProviderScreen, ModelSettingsEditor
from lancher_code.tui_views.theme import apply_theme

TAB_IDS = ("model", "mcp", "permissions", "ui")


@dataclass(frozen=True, slots=True)
class SettingsResult:
    saved: bool
    restart_required: bool = False
    config: AppConfig | None = None
    runtime_applied: bool = False


class DiscardChangesScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "keep", "继续编辑")]
    CSS = """
    DiscardChangesScreen { align: center middle; background: $background 80%; }
    #discard-box { width: 54; max-width: 95%; height: auto; padding: 1 2; background: $surface; border: solid $panel; }
    #discard-box Static { height: auto; }
    #discard-actions { height: auto; margin-top: 1; }
    #discard-actions Button { min-width: 8; margin-right: 1; background: transparent; color: $text; text-style: none; border: none; }
    #discard-actions Button:focus { background: $foreground; color: $background; text-style: bold; }
    DiscardChangesScreen.-narrow #discard-actions { layout: vertical; }
    DiscardChangesScreen.-narrow #discard-actions Button { width: 1fr; margin-right: 0; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="discard-box"):
            yield Static("还有未保存的修改。离开只会放弃当前编辑；此前保存的设置会保留。")
            with Horizontal(id="discard-actions"):
                yield Button("继续编辑", id="keep-editing")
                yield Button("放弃本页修改", variant="error", id="confirm-discard")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 50, "-narrow")

    def on_resize(self) -> None:
        self.set_class(self.size.width < 50, "-narrow")

    @on(Button.Pressed, "#keep-editing")
    def keep_editing(self) -> None:
        self.dismiss(False)

    def action_keep(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-discard")
    def discard(self) -> None:
        self.dismiss(True)


class SettingsScreen(Screen[SettingsResult]):
    """逐条提交设置；已保存状态不会被后来取消的草稿覆盖。"""

    BINDINGS = [Binding("escape", "close_settings", "返回", show=False), Binding("ctrl+s", "save_settings", "保存", show=False)]
    CSS = """
    SettingsScreen { background: $background; color: $text; align-horizontal: center; }
    #settings-root { width: 100%; max-width: 104; height: 100%; padding: 1 2; }
    #settings-title { height: auto; color: $text; text-style: bold; }
    #settings-tabs { height: 2; margin-top: 1; border-bottom: solid $panel; }
    .settings-tab { width: 1fr; height: 1; min-width: 0; border: none; padding: 0; background: transparent; color: $text-muted; }
    .settings-tab.-selected { color: $text; text-style: bold; }
    .settings-tab:focus, .settings-tab.-selected:focus { background: $foreground; color: $background; text-style: bold; }
    #settings-error { color: $error; height: auto; display: none; }
    #settings-restart { color: $warning; height: auto; display: none; }
    #settings-notice { color: $success; height: auto; display: none; }
    #settings-pages { height: 1fr; margin-top: 1; }
    .settings-page { height: auto; display: none; }
    .settings-page.-active { display: block; }
    .field { height: auto; margin-bottom: 1; }
    .field-label { height: auto; color: $text-muted; }
    .form, .list-region, .row-actions { height: auto; }
    .editor-title { height: auto; text-style: bold; color: $text; margin-bottom: 1; }
    .scope-note { height: auto; color: $text-muted; margin: 1 0; }
    Input, Select { width: 100%; height: 3; border: none; border-bottom: solid $panel; background: transparent; color: $text; padding: 0 1; }
    Input:focus, Select:focus { border-bottom: solid $primary; }
    Select > SelectCurrent { background: transparent; border: none; color: $text; }
    Select > SelectOverlay { background: $surface; color: $text; border: solid $primary; }
    Checkbox { height: 3; background: transparent; color: $text; }
    Collapsible { height: auto; background: transparent; border: none; padding: 0; }
    Button { background: transparent; color: $text; text-style: none; border: none; min-width: 8; height: 2; padding: 0 1; }
    Button:hover { background: $surface; color: $text; }
    Button:focus { background: $foreground; color: $background; text-style: bold; }
    .link-button { width: auto; max-width: 100%; margin: 0 0 1 0; }
    #model-catalog .link-button { height: 1; margin: 0; color: $text-muted; }
    #model-catalog .link-button:focus { background: $foreground; color: $background; }
    .model-usage-row { height: 1; width: 100%; }
    .model-usage-row .link-button { width: 8; min-width: 8; padding: 0 1; }
    #catalog-heading { height: 1; margin-top: 1; text-style: bold; }
    #current-model-summary, #default-model-summary { height: 1; width: 1fr; }
    #settings-root.-narrow #model-catalog .link-button { margin: 0; }
    #settings-root.-narrow .catalog-help { margin: 0; }
    .danger-button { color: $text-muted; width: auto; max-width: 100%; }
    .danger-button:focus { background: $foreground; color: $background; }
    DataTable { height: 10; min-height: 4; background: transparent; color: $text; }
    DataTable > .datatable--header { color: $text; background: $surface; text-style: bold; }
    DataTable > .datatable--cursor { background: transparent; text-style: none; }
    DataTable:focus > .datatable--cursor { background: $foreground; color: $background; text-style: bold; }
    #model-tree:focus { background-tint: transparent; }
    #model-tree > .tree--cursor { background: transparent; text-style: none; }
    #model-tree:focus > .tree--cursor { background: $foreground; color: $background; text-style: none; }
    #model-tree > .tree--highlight-line { background: transparent; }
    #settings-actions { height: auto; border-top: solid $panel; padding-top: 1; }
    #settings-actions Button { margin-right: 1; }
    #settings-save { color: $primary; text-style: bold; }
    #settings-save:focus { background: $foreground; color: $background; }
    #settings-cancel { color: $text-muted; }
    #settings-cancel:focus { background: $foreground; color: $background; }
    #settings-help { height: auto; color: $text-muted; }
    #settings-root.-narrow { padding: 0 1; }
    #settings-root.-narrow #settings-tabs { margin-top: 0; }
    #settings-root.-narrow .settings-tab.-selected { text-style: bold underline; }
    #settings-root.-narrow #settings-actions { padding-top: 0; }
    #settings-root.-narrow #settings-help { display: none; }
    #settings-root.-narrow #settings-pages { margin-top: 0; }
    """

    def __init__(self, service: SettingsService, current_model_ref: str | None = None,
                 on_models_saved: Callable[[AppConfig], str | None] | None = None,
                 on_model_selected: Callable[[str], str | None] | None = None,
                 on_ui_saved: Callable[[UIConfig], None] | None = None) -> None:
        super().__init__()
        self.service = service
        self.current_model_ref = current_model_ref
        self._on_models_saved, self._on_model_selected, self._on_ui_saved = on_models_saved, on_model_selected, on_ui_saved
        self.snapshot: SettingsSnapshot | None = None
        self._active_tab = "model"
        self._domain_editing = False
        self._form_baseline: dict[str, Any] = {}
        self._mcp_scope = "global"
        self._rule_scope = "project"
        self._editing_mcp_name: str | None = None
        self._editing_rule_index: int | None = None
        self._saved = False
        self._restart_required = service.mcp_restart_required
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
                with Vertical(id="page-mcp", classes="settings-page"):
                    with Vertical(id="mcp-list", classes="list-region"):
                        yield Select((("全局", "global"), ("当前项目", "project")), value="global", allow_blank=False, id="mcp-scope")
                        yield Static("项目中同名服务器完整覆盖全局条目。", classes="scope-note")
                        yield DataTable(id="mcp-table", cursor_type="row")
                        yield Button("＋ 添加服务器", id="mcp-new", classes="link-button")
                    with Vertical(id="mcp-editor", classes="form"):
                        yield Static("", id="mcp-editor-title", classes="editor-title", markup=False)
                        yield Static("", id="mcp-editor-scope", classes="scope-note", markup=False)
                        yield from self._field("服务器名称", Input(id="mcp-name"))
                        yield from self._field("连接类型", Select((("stdio", "stdio"), ("HTTP", "http")), value="stdio", allow_blank=False, id="mcp-type"))
                        yield Checkbox("启用", value=True, id="mcp-enabled")
                        yield Static("启动命令", id="mcp-target-label", classes="field-label")
                        yield Input(id="mcp-target")
                        with Collapsible(title="高级：参数与环境变量 / 请求头", collapsed=True, id="mcp-advanced"):
                            with Vertical(id="mcp-args-field", classes="field"):
                                yield Static("Args（YAML 字符串数组）", classes="field-label")
                                yield Input(value="[]", id="mcp-args")
                            yield from self._field("Env / Headers（YAML 字符串对象）", Input(value="{}", id="mcp-map"))
                        yield Button("删除服务器", id="mcp-delete", classes="danger-button")
                with Vertical(id="page-permissions", classes="settings-page"):
                    with Vertical(id="rules-list", classes="list-region"):
                        yield Select((("当前项目", "project"), ("全局", "user")), value="project", allow_blank=False, id="rule-scope")
                        yield Static("会话规则优先于项目，项目优先于全局；同层最后匹配的规则生效。", classes="scope-note")
                        yield DataTable(id="rules-table", cursor_type="row")
                        yield Button("＋ 添加权限规则", id="rule-new", classes="link-button")
                    with Vertical(id="rule-editor", classes="form"):
                        yield Static("权限规则", classes="editor-title")
                        yield Static("", id="rule-editor-scope", classes="scope-note")
                        yield from self._field("匹配表达式", Input(placeholder="例如 RunCommand(git status)", id="rule-match"))
                        yield from self._field("匹配方式", Select((("精确匹配", "exact"), ("通配规则（glob）", "glob"), ("旧版规则（保留兼容）", "legacy")), value="exact", allow_blank=False, id="rule-match-kind"))
                        yield Static("精确匹配只授权完整目标；通配规则可覆盖多个目标。旧版仅用于保留已有规则。", classes="scope-note")
                        yield from self._field("处理方式", Select((("允许", "allow"), ("拒绝", "deny")), value="allow", allow_blank=False, id="rule-result"))
                        with Horizontal(classes="row-actions"):
                            yield Button("上移", id="rule-up")
                            yield Button("下移", id="rule-down")
                            yield Button("删除", id="rule-delete", classes="danger-button")
                with Vertical(id="page-ui", classes="settings-page"):
                    yield Static("外观与忙时输入", classes="editor-title")
                    yield from self._field("终端外观", Select((("深色", "dark"), ("浅色", "light")), value="dark", allow_blank=False, id="ui-theme"))
                    yield from self._field("工作中按 Enter", Select((("排到下一轮", "follow_up"), ("补充当前任务", "steer"), ("仅保留草稿", "draft")), value="follow_up", allow_blank=False, id="ui-busy-enter"))
                    yield Static("只影响工作中的输入；闲置时 Enter 仍发送。保存后生效。", classes="scope-note")
            with Horizontal(id="settings-actions"):
                yield Button("‹ 返回对话", id="settings-cancel")
                yield Button("保存", id="settings-save")
            yield Static("Enter 打开 · Esc 返回 · Ctrl+S 保存当前编辑", id="settings-help")

    @staticmethod
    def _field(label: str, widget: Any) -> ComposeResult:
        with Vertical(classes="field"):
            yield Static(label, classes="field-label")
            yield widget

    def on_mount(self) -> None:
        self.query_one("#mcp-table", DataTable).add_columns("名称", "类型", "状态")
        self.query_one("#rules-table", DataTable).add_columns("顺序", "匹配表达式", "匹配方式", "结果")
        try:
            self.snapshot = self.service.load()
            self.current_model_ref = self.current_model_ref or self.snapshot.config.default_model
            apply_theme(self.app, getattr(self.snapshot.config.ui, "theme", "dark"))
            self.query_one(ModelSettingsEditor).load_config(self.snapshot.config, self.current_model_ref)
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
        return self.query_one(ModelSettingsEditor).editing if self._active_tab == "model" else self._domain_editing

    def _dirty(self) -> bool:
        if self._active_tab == "model":
            return self.query_one(ModelSettingsEditor).dirty
        return self._domain_editing and self._form_values() != self._form_baseline

    def _form_values(self) -> dict[str, Any]:
        selector = {"mcp":"#mcp-editor", "permissions":"#rule-editor", "ui":"#page-ui"}.get(self._active_tab)
        return {widget.id: widget.value for widget in self.query_one(selector).query("Input, Select, Checkbox")} if selector else {}

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
        self._domain_editing = tab_id == "ui"
        self._show_error("")
        self.query_one("#settings-notice", Static).update("")
        self.query_one("#settings-notice").display = False
        self.query_one(ModelSettingsEditor).show_catalog()
        for candidate in TAB_IDS:
            self.query_one(f"#page-{candidate}").set_class(candidate == tab_id, "-active")
            self.query_one(f"#tab-{candidate}").set_class(candidate == tab_id, "-selected")
        self._refresh_tab_labels()
        self.query_one("#mcp-list").display = True
        self.query_one("#mcp-editor").display = False
        self.query_one("#rules-list").display = True
        self.query_one("#rule-editor").display = False
        if tab_id == "mcp":
            self._refresh_mcp_table()
        elif tab_id == "permissions":
            self._refresh_rules_table()
        elif tab_id == "ui":
            with self.prevent(Select.Changed):
                self.query_one("#ui-theme", Select).value = getattr(self.snapshot.config.ui, "theme", "dark")
                self.query_one("#ui-busy-enter", Select).value = getattr(self.snapshot.config.ui, "busy_enter_action", "follow_up")
            self._form_baseline = self._form_values()
        self._refresh_actions()
        self.query_one("#settings-pages", VerticalScroll).scroll_home(animate=False)

    @on(ModelSettingsEditor.ViewChanged)
    def model_view_changed(self) -> None:
        self._refresh_actions()
        self.query_one("#settings-pages", VerticalScroll).scroll_home(animate=False)

    def _refresh_actions(self) -> None:
        self.query_one("#settings-restart", Static).update("MCP 有已保存的更改 · 重启后生效" if self._restart_required else "")
        self.query_one("#settings-restart").display = self._restart_required
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
            if self.current_model_ref not in iter_model_refs(committed):
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

    @on(Select.Changed, "#mcp-scope")
    def mcp_scope_changed(self, event: Select.Changed) -> None:
        if event.value in {"global", "project"}:
            self._mcp_scope = event.value
            self._refresh_mcp_table()

    def _mcp_servers(self) -> dict[str, dict[str, Any]]:
        return self.snapshot.global_mcp if self._mcp_scope == "global" else self.snapshot.project_mcp

    def _refresh_mcp_table(self) -> None:
        table = self.query_one("#mcp-table", DataTable); table.clear()
        if self.snapshot:
            for name, server in self._mcp_servers().items():
                table.add_row(Text(name), str(server.get("type", "")), "启用" if server.get("enabled", True) else "停用", key=name)

    @on(DataTable.RowSelected, "#mcp-table")
    def select_mcp(self, event: DataTable.RowSelected) -> None:
        self.edit_mcp(str(event.row_key.value))

    @on(Button.Pressed, "#mcp-new")
    def new_mcp(self) -> None:
        self.edit_mcp()

    def edit_mcp(self, name: str | None = None) -> None:
        server = self._mcp_servers().get(name, {})
        self._editing_mcp_name = name
        self.query_one("#mcp-list").display = False
        self.query_one("#mcp-editor").display = True
        self.query_one("#mcp-editor-title", Static).update(f"编辑 MCP · {name}" if name else "添加 MCP 服务器")
        self.query_one("#mcp-editor-scope", Static).update(("全局" if self._mcp_scope == "global" else "当前项目") + " · 保存后重启生效")
        kind = server.get("type", "stdio")
        with self.prevent(Select.Changed):
            self.query_one("#mcp-type", Select).value = kind
        self.query_one("#mcp-name", Input).value = name or ""
        self.query_one("#mcp-enabled", Checkbox).value = server.get("enabled", True)
        self.query_one("#mcp-target", Input).value = str(server.get("command" if kind == "stdio" else "url", ""))
        self.query_one("#mcp-args", Input).value = yaml.safe_dump(server.get("args", []), default_flow_style=True).strip()
        self.query_one("#mcp-map", Input).value = yaml.safe_dump(server.get("env" if kind == "stdio" else "headers", {}), default_flow_style=True).strip()
        self.query_one("#mcp-delete").display = name is not None
        self._sync_mcp_kind()
        self._begin_form()

    @on(Select.Changed, "#mcp-type")
    def mcp_kind_changed(self) -> None:
        self._sync_mcp_kind()

    def _sync_mcp_kind(self) -> None:
        stdio = self.query_one("#mcp-type", Select).value == "stdio"
        self.query_one("#mcp-target-label", Static).update("启动命令" if stdio else "服务器 URL")
        self.query_one("#mcp-args-field").display = stdio

    def _begin_form(self) -> None:
        self._domain_editing = True
        self._form_baseline = self._form_values()
        self._refresh_actions()
        self.query_one("#settings-pages", VerticalScroll).scroll_home(animate=False)
        selector = "#mcp-name" if self._active_tab == "mcp" else "#rule-match"
        self.query_one(selector, Input).focus()

    def _save_mcp(self, delete: bool = False) -> None:
        servers = deepcopy(self._mcp_servers())
        name = self.query_one("#mcp-name", Input).value.strip()
        if delete:
            servers.pop(self._editing_mcp_name, None)
        else:
            if name != self._editing_mcp_name and name in servers:
                raise SettingsError("已有同名服务器，请使用不同名称。")
            kind = self.query_one("#mcp-type", Select).value
            args = yaml.safe_load(self.query_one("#mcp-args", Input).value) if kind == "stdio" else []
            mapping = yaml.safe_load(self.query_one("#mcp-map", Input).value)
            # 保留当前条目中表单未管理的附加字段。
            server = deepcopy(servers.get(self._editing_mcp_name, {}))
            for key in ("command", "url", "args", "env", "headers"):
                server.pop(key, None)
            server.update(type=kind, enabled=self.query_one("#mcp-enabled", Checkbox).value)
            target = self.query_one("#mcp-target", Input).value.strip()
            if kind == "stdio":
                server.update(command=target, args=[] if args is None else args, env={} if mapping is None else mapping)
            else:
                server.update(url=target, headers={} if mapping is None else mapping)
            if self._editing_mcp_name and self._editing_mcp_name != name:
                servers.pop(self._editing_mcp_name)
            servers[name] = server
        changed = servers != self._mcp_servers()
        self.service.save_mcp(self._mcp_scope, servers)
        if self._mcp_scope == "global": self.snapshot.global_mcp = servers
        else: self.snapshot.project_mcp = servers
        self._restart_required |= changed
        self._saved = True
        self._show_tab("mcp"); self._notice("MCP 已保存，重启后生效。" if changed else "MCP 设置未改变。")

    @on(Button.Pressed, "#mcp-delete")
    def delete_mcp(self) -> None:
        self._guard(lambda: self._save_with_error(lambda: self._save_mcp(delete=True)))

    @on(Select.Changed, "#rule-scope")
    def rule_scope_changed(self, event: Select.Changed) -> None:
        if event.value in {"project", "user"}:
            self._rule_scope = event.value; self._refresh_rules_table()

    def _rules(self) -> list[PermissionRule]:
        return self.snapshot.project_rules if self._rule_scope == "project" else self.snapshot.user_rules

    def _refresh_rules_table(self) -> None:
        table = self.query_one("#rules-table", DataTable); table.clear()
        if self.snapshot:
            for i, rule in enumerate(self._rules()):
                table.add_row(str(i + 1), Text(rule.match), {"exact":"精确", "glob":"通配", "legacy":"旧版"}[rule.match_kind], "允许" if rule.result == "allow" else "拒绝", key=str(i))

    @on(DataTable.RowSelected, "#rules-table")
    def select_rule(self, event: DataTable.RowSelected) -> None:
        self.edit_rule(int(str(event.row_key.value)))

    @on(Button.Pressed, "#rule-new")
    def new_rule(self) -> None:
        self.edit_rule()

    def edit_rule(self, index: int | None = None) -> None:
        self._editing_rule_index = index
        rule = self._rules()[index] if index is not None else PermissionRule("", "allow", self._rule_scope, match_kind="exact")
        self.query_one("#rules-list").display = False
        self.query_one("#rule-editor").display = True
        self.query_one("#rule-editor-scope", Static).update(("当前项目" if self._rule_scope == "project" else "全局") + " · 保存后立即生效")
        self.query_one("#rule-match", Input).value = rule.match
        self.query_one("#rule-result", Select).value = rule.result
        self.query_one("#rule-match-kind", Select).value = rule.match_kind
        for action in ("up", "down", "delete"):
            self.query_one(f"#rule-{action}").display = index is not None
        self._begin_form()

    def _commit_rules(self, rules: list[PermissionRule]) -> None:
        self.service.save_rules(self._rule_scope, rules)
        if self._rule_scope == "project": self.snapshot.project_rules = rules
        else: self.snapshot.user_rules = rules
        self._saved = True
        self._show_tab("permissions"); self._notice("权限规则已保存。")

    @on(Button.Pressed, "#rule-up")
    @on(Button.Pressed, "#rule-down")
    @on(Button.Pressed, "#rule-delete")
    def rule_action(self, event: Button.Pressed) -> None:
        index = self._editing_rule_index
        if index is None: return
        action = event.button.id.removeprefix("rule-")
        def commit() -> None:
            rules = deepcopy(self._rules())
            if action == "delete": rules.pop(index)
            else:
                target = index + (-1 if action == "up" else 1)
                if not 0 <= target < len(rules): return
                rules[index], rules[target] = rules[target], rules[index]
            self._save_with_error(lambda: self._commit_rules(rules))
        self._guard(commit)

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
            elif self._active_tab == "mcp": self._save_mcp()
            elif self._active_tab == "permissions":
                rules = deepcopy(self._rules())
                rule = PermissionRule(self.query_one("#rule-match", Input).value.strip(), self.query_one("#rule-result", Select).value, self._rule_scope, match_kind=self.query_one("#rule-match-kind", Select).value)
                if self._editing_rule_index is None: rules.append(rule)
                else: rules[self._editing_rule_index] = rule
                self._commit_rules(rules)
            else:
                ui = deepcopy(self.snapshot.config.ui)
                ui.theme = self.query_one("#ui-theme", Select).value
                ui.busy_enter_action = self.query_one("#ui-busy-enter", Select).value
                self.snapshot.config = self.service.save_ui(ui)
                self._saved = True
                apply_theme(self.app, ui.theme)
                callback_error = None
                try:
                    if self._on_ui_saved: self._on_ui_saved(deepcopy(ui))
                except Exception as exc: callback_error = f"偏好已保存，当前界面更新失败：{exc}"
                self._show_tab("model"); self._notice("外观与输入偏好已保存。")
                if callback_error: self._show_error(callback_error)
        self._save_with_error(save)

    @on(Button.Pressed, "#settings-cancel")
    def cancel_pressed(self) -> None:
        self.action_close_settings()

    def action_close_settings(self) -> None:
        def leave() -> None:
            if self._editing:
                self._show_tab(self._active_tab if self._active_tab != "ui" else "model")
            else:
                self.dismiss(SettingsResult(self._saved, self._restart_required, deepcopy(self.snapshot.config) if self.snapshot else None, self._runtime_applied))
        self._guard(leave)

    def _show_error(self, message: str) -> None:
        self.query_one("#settings-error", Static).update(message)
        self.query_one("#settings-error").display = bool(message)

    def _notice(self, message: str) -> None:
        self._show_error("")
        self.query_one("#settings-notice", Static).update(message)
        self.query_one("#settings-notice").display = bool(message)
