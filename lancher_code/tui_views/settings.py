from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from lancher_code.models import PermissionRule, ProviderConfig, ThinkingConfig
from lancher_code.settings_service import SettingsError, SettingsService, SettingsSnapshot


TAB_IDS = ("model", "mcp", "project-permissions", "global-permissions")


@dataclass(frozen=True, slots=True)
class SettingsResult:
    saved: bool
    restart_required: bool = False


class DiscardChangesScreen(ModalScreen[bool]):
    CSS = """
    DiscardChangesScreen { align: center middle; background: #08111b 70%; }
    #discard-box { width: 52; height: auto; padding: 1 2; background: #0f1a26; border: solid #4b6f97; }
    #discard-actions { height: auto; margin-top: 1; align-horizontal: right; }
    #discard-actions Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="discard-box"):
            yield Static("有尚未保存的设置，确定要放弃吗？")
            with Horizontal(id="discard-actions"):
                yield Button("继续编辑", id="keep-editing")
                yield Button("放弃修改", variant="error", id="confirm-discard")

    @on(Button.Pressed, "#keep-editing")
    def keep_editing(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-discard")
    def discard(self) -> None:
        self.dismiss(True)


class SettingsScreen(Screen[SettingsResult]):
    BINDINGS = [
        Binding("escape", "close_settings", "返回", show=False),
        Binding("ctrl+s", "save_settings", "保存", show=False),
        Binding("left", "previous_tab", "上一标签", show=False),
        Binding("right", "next_tab", "下一标签", show=False),
    ]

    CSS = """
    SettingsScreen { background: transparent; color: #f2f2f2; }
    #settings-root { width: 100%; height: 100%; padding: 1 2; }
    #settings-title { height: 2; color: #f2f2f2; text-style: bold; }
    #settings-tabs { height: 2; border-bottom: solid #29445f; }
    .settings-tab {
        width: 1fr;
        height: 1;
        min-width: 8;
        padding: 0 1;
        border: none;
        background: transparent;
        color: #a8b9cc;
        text-style: none;
        content-align: center middle;
    }
    .settings-tab:hover, .settings-tab:focus { background: transparent; color: #f2f2f2; text-style: bold; }
    .settings-tab.-active { color: #73b6ff; background: transparent; border-bottom: solid #73b6ff; text-style: bold; }
    #settings-error { height: auto; min-height: 1; color: #ff7b72; }
    #settings-pages { height: 1fr; }
    .settings-page { width: 100%; height: 100%; padding: 1 0 0 0; display: none; }
    .settings-page.-active { display: block; }
    .field { height: auto; margin-bottom: 1; }
    .field-label { height: 1; color: #7f9ab8; }
    Input, Select {
        width: 100%;
        height: 3;
        border: tall #29445f;
        background: #0f1a26;
        color: #f2f2f2;
    }
    Input { padding: 0 1; }
    Select { padding: 0; }
    Input:focus, Select:focus { border: tall #73b6ff; background: #0f1a26; }
    Select > SelectCurrent { height: 3; padding: 0 1; border: none; background: #0f1a26; color: #f2f2f2; }
    Select > SelectCurrent Static#label,
    Select > SelectCurrent.-has-value Static#label { color: #f2f2f2; background: transparent; text-opacity: 1; }
    Select > SelectCurrent .arrow { color: #97adc7; background: transparent; text-opacity: 1; }
    Select > SelectOverlay { background: #0f1a26; color: #f2f2f2; border: solid #29445f; }
    Checkbox { background: transparent; color: #c8d5e3; height: 3; padding: 0 1; }
    Checkbox > .toggle--label { color: #c8d5e3; background: transparent; text-opacity: 1; }
    Checkbox > .toggle--button { color: #78d98a; background: #0f1a26; text-opacity: 1; }
    Checkbox:focus { background: #142437; color: #f2f2f2; }
    Checkbox:focus > .toggle--label { color: #f2f2f2; background: transparent; }
    #settings-actions { height: 4; align-horizontal: right; border-top: solid #29445f; padding: 0; }
    #settings-actions Button, .row-actions Button {
        height: 3;
        min-width: 10;
        margin-left: 1;
        border: tall #29445f;
        background: #0f1a26;
        color: #c8d5e3;
        text-opacity: 1;
    }
    #settings-actions Button:hover, #settings-actions Button:focus,
    .row-actions Button:hover, .row-actions Button:focus { border: tall #73b6ff; background: #142437; color: #f2f2f2; }
    #settings-save, .row-actions Button.-primary { background: #174f78; color: #f2f2f2; text-style: bold; }
    .row-actions Button.-error { color: #ff9b9b; }
    .row-actions { height: 3; align-horizontal: right; padding: 0; }
    DataTable { height: 1fr; min-height: 6; background: transparent; border: none; color: #c8d5e3; }
    DataTable:focus { border: none; }
    .split { height: 1fr; }
    .editor { width: 42%; min-width: 32; padding-left: 1; }
    .table-region { width: 1fr; }
    #mcp-layer-note, .scope-note { color: #7f9ab8; height: auto; margin: 1 0; }
    #restart-note { color: #7f9ab8; height: auto; }
    #settings-root.-narrow { padding: 1; }
    #settings-root.-narrow .split { layout: vertical; overflow-y: auto; }
    #settings-root.-narrow .table-region, #settings-root.-narrow .editor { width: 100%; min-width: 0; height: auto; padding-left: 0; }
    #settings-root.-narrow DataTable { height: 10; }
    """

    class TabChosen(Message):
        def __init__(self, tab_id: str) -> None:
            self.tab_id = tab_id
            super().__init__()

    def __init__(self, service: SettingsService) -> None:
        super().__init__()
        self.service = service
        self.snapshot: SettingsSnapshot | None = None
        self._original: SettingsSnapshot | None = None
        self._active_tab = "model"
        self._mcp_scope: Literal["global", "project"] = "global"
        self._editing_mcp_name: str | None = None
        self._editing_rule_index: dict[str, int | None] = {"project": None, "user": None}

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-root"):
            yield Static("LanCher Code · 设置", id="settings-title")
            with Horizontal(id="settings-tabs"):
                for tab_id, label in zip(TAB_IDS, ("模型设置", "MCP 服务器", "项目权限", "全局权限")):
                    yield Button(label, id=f"tab-{tab_id}", classes="settings-tab")
            yield Static("", id="settings-error")
            with Vertical(id="settings-pages"):
                yield from self._compose_model()
                yield from self._compose_mcp()
                yield from self._compose_permissions("project", "project-permissions")
                yield from self._compose_permissions("user", "global-permissions")
            yield Static("模型与 MCP 的修改将在重启 LanCher Code 后生效。", id="restart-note")
            with Horizontal(id="settings-actions"):
                yield Button("取消", id="settings-cancel")
                yield Button("保存", variant="primary", id="settings-save")

    def _compose_model(self) -> ComposeResult:
        with VerticalScroll(id="page-model", classes="settings-page"):
            yield from self._field("提供商协议", Select((("OpenAI", "openai"), ("Claude", "claude")), allow_blank=False, id="model-protocol"))
            yield from self._field("模型名称", Input(id="model-name"))
            yield from self._field("Base URL", Input(id="model-base-url"))
            yield from self._field("API Key（留空不会清除原值）", Input(password=True, id="model-api-key"))
            yield from self._field("请求超时（秒）", Input(id="model-timeout", type="number"))
            yield Checkbox("启用 Claude thinking", id="model-thinking")
            yield from self._field("Thinking budget tokens（可选）", Input(id="model-thinking-budget", type="integer"))

    def _compose_mcp(self) -> ComposeResult:
        with Vertical(id="page-mcp", classes="settings-page"):
            yield Select((("全局配置", "global"), ("项目配置", "project")), value="global", allow_blank=False, id="mcp-scope")
            yield Static("项目中的同名服务器会完整覆盖全局配置。", id="mcp-layer-note")
            with Horizontal(classes="split"):
                with Vertical(classes="table-region"):
                    yield DataTable(id="mcp-table", cursor_type="row")
                with VerticalScroll(classes="editor"):
                    yield from self._field("名称", Input(id="mcp-name"))
                    yield from self._field("类型", Select((("stdio", "stdio"), ("http", "http")), value="stdio", allow_blank=False, id="mcp-type"))
                    yield Checkbox("启用", value=True, id="mcp-enabled")
                    yield from self._field("Command 或 URL", Input(id="mcp-target"))
                    yield from self._field("Args（YAML 数组）", Input(value="[]", id="mcp-args"))
                    yield from self._field("Env / Headers（YAML 对象）", Input(value="{}", id="mcp-map"))
                    with Horizontal(classes="row-actions"):
                        yield Button("新建", id="mcp-new")
                        yield Button("保存条目", variant="primary", id="mcp-apply")
                        yield Button("删除", variant="error", id="mcp-delete")

    def _compose_permissions(self, scope: Literal["project", "user"], page_id: str) -> ComposeResult:
        label = "项目规则优先于全局规则。" if scope == "project" else "全局规则在没有项目匹配项时生效。"
        with Vertical(id=f"page-{page_id}", classes="settings-page"):
            yield Static(label, classes="scope-note")
            with Horizontal(classes="split"):
                with Vertical(classes="table-region"):
                    yield DataTable(id=f"{scope}-rules-table", cursor_type="row")
                with Vertical(classes="editor"):
                    yield from self._field("Match", Input(id=f"{scope}-rule-match"))
                    yield from self._field("结果", Select((("允许", "allow"), ("拒绝", "deny")), value="allow", allow_blank=False, id=f"{scope}-rule-result"))
                    with Horizontal(classes="row-actions"):
                        yield Button("新建", id=f"{scope}-rule-new")
                        yield Button("保存条目", variant="primary", id=f"{scope}-rule-apply")
                    with Horizontal(classes="row-actions"):
                        yield Button("上移", id=f"{scope}-rule-up")
                        yield Button("下移", id=f"{scope}-rule-down")
                        yield Button("删除", variant="error", id=f"{scope}-rule-delete")

    @staticmethod
    def _field(label: str, widget: Any) -> ComposeResult:
        with Vertical(classes="field"):
            yield Static(label, classes="field-label")
            yield widget

    def on_mount(self) -> None:
        for table_id, columns in (
            ("mcp-table", ("名称", "类型", "状态", "来源")),
            ("project-rules-table", ("Match", "结果")),
            ("user-rules-table", ("Match", "结果")),
        ):
            self.query_one(f"#{table_id}", DataTable).add_columns(*columns)
        try:
            self.snapshot = self.service.load()
            self._original = deepcopy(self.snapshot)
            self._load_widgets()
            self._show_tab("model")
        except SettingsError as exc:
            self._show_error(str(exc))
            self.query_one("#settings-save", Button).disabled = True
        self._refresh_responsive_layout()

    def on_resize(self) -> None:
        self._refresh_responsive_layout()

    def _refresh_responsive_layout(self) -> None:
        self.query_one("#settings-root").set_class(self.size.width < 75, "-narrow")

    def _load_widgets(self) -> None:
        assert self.snapshot is not None
        provider = self.snapshot.config.provider
        self.query_one("#model-protocol", Select).value = provider.protocol
        self.query_one("#model-name", Input).value = provider.model
        self.query_one("#model-base-url", Input).value = provider.base_url
        self.query_one("#model-api-key", Input).value = ""
        self.query_one("#model-timeout", Input).value = str(provider.timeout_seconds)
        self.query_one("#model-thinking", Checkbox).value = bool(provider.thinking and provider.thinking.enabled)
        self.query_one("#model-thinking-budget", Input).value = str(provider.thinking.budget_tokens or "") if provider.thinking else ""
        self._refresh_mcp_table()
        self._refresh_rules_table("project")
        self._refresh_rules_table("user")

    @on(Button.Pressed, ".settings-tab")
    def choose_tab(self, event: Button.Pressed) -> None:
        self._show_tab(event.button.id.removeprefix("tab-"))

    def _show_tab(self, tab_id: str) -> None:
        self._active_tab = tab_id
        for candidate in TAB_IDS:
            self.query_one(f"#page-{candidate}").set_class(candidate == tab_id, "-active")
            self.query_one(f"#tab-{candidate}").set_class(candidate == tab_id, "-active")

    def action_previous_tab(self) -> None:
        index = TAB_IDS.index(self._active_tab)
        self._show_tab(TAB_IDS[(index - 1) % len(TAB_IDS)])

    def action_next_tab(self) -> None:
        index = TAB_IDS.index(self._active_tab)
        self._show_tab(TAB_IDS[(index + 1) % len(TAB_IDS)])

    @on(Select.Changed, "#mcp-scope")
    def change_mcp_scope(self, event: Select.Changed) -> None:
        if event.value in {"global", "project"}:
            self._mcp_scope = event.value
            self._editing_mcp_name = None
            self._clear_mcp_editor()
            self._refresh_mcp_table()

    def _mcp_servers(self) -> dict[str, dict[str, Any]]:
        assert self.snapshot is not None
        return self.snapshot.global_mcp if self._mcp_scope == "global" else self.snapshot.project_mcp

    def _refresh_mcp_table(self) -> None:
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        if self.snapshot is None:
            return
        for name, server in self._mcp_servers().items():
            table.add_row(name, str(server.get("type", "")), "启用" if server.get("enabled", True) else "停用", "全局" if self._mcp_scope == "global" else "项目", key=name)

    @on(DataTable.RowSelected, "#mcp-table")
    def select_mcp(self, event: DataTable.RowSelected) -> None:
        name = str(event.row_key.value)
        server = self._mcp_servers().get(name)
        if server is None:
            return
        self._editing_mcp_name = name
        self.query_one("#mcp-name", Input).value = name
        self.query_one("#mcp-type", Select).value = server.get("type", "stdio")
        self.query_one("#mcp-enabled", Checkbox).value = server.get("enabled", True)
        kind = server.get("type", "stdio")
        self.query_one("#mcp-target", Input).value = str(server.get("command" if kind == "stdio" else "url", ""))
        self.query_one("#mcp-args", Input).value = yaml.safe_dump(server.get("args", []), default_flow_style=True).strip()
        mapping = server.get("env" if kind == "stdio" else "headers", {})
        self.query_one("#mcp-map", Input).value = yaml.safe_dump(mapping, default_flow_style=True).strip()

    @on(Button.Pressed, "#mcp-new")
    def new_mcp(self) -> None:
        self._editing_mcp_name = None
        self._clear_mcp_editor()
        self.query_one("#mcp-name", Input).focus()

    def _clear_mcp_editor(self) -> None:
        for widget_id in ("mcp-name", "mcp-target"):
            self.query_one(f"#{widget_id}", Input).value = ""
        self.query_one("#mcp-args", Input).value = "[]"
        self.query_one("#mcp-map", Input).value = "{}"
        self.query_one("#mcp-enabled", Checkbox).value = True

    @on(Button.Pressed, "#mcp-apply")
    def apply_mcp(self) -> None:
        try:
            name = self.query_one("#mcp-name", Input).value.strip()
            kind = self.query_one("#mcp-type", Select).value
            target = self.query_one("#mcp-target", Input).value.strip()
            args = yaml.safe_load(self.query_one("#mcp-args", Input).value) or []
            mapping = yaml.safe_load(self.query_one("#mcp-map", Input).value) or {}
            if not name or kind not in {"stdio", "http"} or not isinstance(args, list) or not isinstance(mapping, dict):
                raise ValueError("请填写合法名称；Args 必须是数组，Env / Headers 必须是对象。")
            server: dict[str, Any] = {"enabled": self.query_one("#mcp-enabled", Checkbox).value, "type": kind}
            if kind == "stdio":
                server.update(command=target, args=args, env=mapping)
            else:
                server.update(url=target, headers=mapping)
            servers = self._mcp_servers()
            if self._editing_mcp_name and self._editing_mcp_name != name:
                servers.pop(self._editing_mcp_name, None)
            servers[name] = server
            self._editing_mcp_name = name
            self._refresh_mcp_table()
            self._show_error("")
        except (ValueError, yaml.YAMLError) as exc:
            self._show_error(str(exc))

    @on(Button.Pressed, "#mcp-delete")
    def delete_mcp(self) -> None:
        if self._editing_mcp_name:
            self._mcp_servers().pop(self._editing_mcp_name, None)
            self._editing_mcp_name = None
            self._clear_mcp_editor()
            self._refresh_mcp_table()

    def _rules(self, scope: Literal["project", "user"]) -> list[PermissionRule]:
        assert self.snapshot is not None
        return self.snapshot.project_rules if scope == "project" else self.snapshot.user_rules

    def _refresh_rules_table(self, scope: Literal["project", "user"]) -> None:
        table = self.query_one(f"#{scope}-rules-table", DataTable)
        table.clear()
        if self.snapshot is not None:
            for index, rule in enumerate(self._rules(scope)):
                table.add_row(rule.match, rule.result, key=str(index))

    @on(DataTable.RowSelected)
    def select_rule(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id or ""
        if table_id not in {"project-rules-table", "user-rules-table"}:
            return
        scope: Literal["project", "user"] = "project" if table_id.startswith("project") else "user"
        index = int(str(event.row_key.value))
        rule = self._rules(scope)[index]
        self._editing_rule_index[scope] = index
        self.query_one(f"#{scope}-rule-match", Input).value = rule.match
        self.query_one(f"#{scope}-rule-result", Select).value = rule.result

    @on(Button.Pressed)
    def handle_rule_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        for scope in ("project", "user"):
            prefix = f"{scope}-rule-"
            if button_id.startswith(prefix):
                self._rule_action(scope, button_id.removeprefix(prefix))
                break

    def _rule_action(self, scope: Literal["project", "user"], action: str) -> None:
        rules = self._rules(scope)
        index = self._editing_rule_index[scope]
        if action == "new":
            self._editing_rule_index[scope] = None
            self.query_one(f"#{scope}-rule-match", Input).value = ""
            self.query_one(f"#{scope}-rule-match", Input).focus()
            return
        if action == "apply":
            match = self.query_one(f"#{scope}-rule-match", Input).value.strip()
            result = self.query_one(f"#{scope}-rule-result", Select).value
            if not match or result not in {"allow", "deny"}:
                self._show_error("权限规则 Match 不能为空。")
                return
            rule = PermissionRule(match=match, result=result, scope=scope)
            if index is None:
                rules.append(rule)
                self._editing_rule_index[scope] = len(rules) - 1
            else:
                rules[index] = rule
        elif index is not None and action == "delete":
            rules.pop(index)
            self._editing_rule_index[scope] = None
        elif index is not None and action in {"up", "down"}:
            target = index + (-1 if action == "up" else 1)
            if 0 <= target < len(rules):
                rules[index], rules[target] = rules[target], rules[index]
                self._editing_rule_index[scope] = target
        self._refresh_rules_table(scope)

    @on(Button.Pressed, "#settings-save")
    def save_pressed(self) -> None:
        self.action_save_settings()

    def action_save_settings(self) -> None:
        if self.snapshot is None:
            return
        try:
            self._collect_model()
            self.service.save(self.snapshot)
        except (SettingsError, ValueError) as exc:
            self._show_error(str(exc))
            field_id = getattr(exc, "field_id", None)
            if field_id:
                try:
                    self.query_one(f"#{field_id}").focus()
                except Exception:
                    pass
            return
        self.dismiss(SettingsResult(saved=True, restart_required=True))

    def _collect_model(self) -> None:
        assert self.snapshot is not None
        old = self.snapshot.config.provider
        protocol = self.query_one("#model-protocol", Select).value
        api_key = self.query_one("#model-api-key", Input).value.strip() or old.api_key
        timeout = float(self.query_one("#model-timeout", Input).value)
        enabled = self.query_one("#model-thinking", Checkbox).value
        budget_text = self.query_one("#model-thinking-budget", Input).value.strip()
        thinking = ThinkingConfig(enabled=enabled, budget_tokens=int(budget_text) if budget_text else None) if protocol == "claude" else None
        self.snapshot.config.provider = ProviderConfig(
            protocol=protocol,  # type: ignore[arg-type]
            model=self.query_one("#model-name", Input).value.strip(),
            base_url=self.query_one("#model-base-url", Input).value.strip(),
            api_key=api_key,
            timeout_seconds=timeout,
            thinking=thinking,
            context_window=self.snapshot.config.provider.context_window,
        )

    @on(Button.Pressed, "#settings-cancel")
    def cancel_pressed(self) -> None:
        self.action_close_settings()

    def action_close_settings(self) -> None:
        dirty = self.snapshot != self._original
        if self.snapshot is not None:
            try:
                self._collect_model()
                dirty = self.snapshot != self._original
            except (TypeError, ValueError):
                dirty = True
        if dirty:
            self.app.push_screen(DiscardChangesScreen(), self._after_discard_prompt)
        else:
            self.dismiss(SettingsResult(saved=False))

    def _after_discard_prompt(self, discard: bool | None) -> None:
        if discard:
            self.dismiss(SettingsResult(saved=False))

    def _show_error(self, message: str) -> None:
        self.query_one("#settings-error", Static).update(message)
