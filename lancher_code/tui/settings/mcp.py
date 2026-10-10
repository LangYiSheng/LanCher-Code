"""MCP 设置的列表、草稿及独立保存。"""
from __future__ import annotations
from copy import deepcopy
from typing import Any
import yaml
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Input, Select, Static
from lancher_code.config.settings import SettingsError, SettingsService
from lancher_code.tui.settings.common import SettingsDomainEditor, DomainSaved, field

class MCPSettingsEditor(SettingsDomainEditor):
    domain = "mcp"
    editor_selector = "#mcp-editor"

    def __init__(self, service: SettingsService) -> None:
        super().__init__(service, id="page-mcp")
        self._mcp_scope = "global"
        self._editing_mcp_name: str | None = None

    def on_mount(self) -> None:
        self.query_one("#mcp-table", DataTable).add_columns("名称", "类型", "状态")

    def show_catalog(self) -> None:
        self.editing = False
        self.query_one("#mcp-list").display = True
        self.query_one("#mcp-editor").display = False
        self._refresh_mcp_table()

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-list", classes="list-region"):
            yield Select((("全局", "global"), ("当前项目", "project")), value="global", allow_blank=False, id="mcp-scope")
            yield Static("项目中同名服务器完整覆盖全局条目。", classes="scope-note")
            yield DataTable(id="mcp-table", cursor_type="row")
            yield Button("＋ 添加服务器", id="mcp-new", classes="link-button")
        with Vertical(id="mcp-editor", classes="form"):
            yield Static("", id="mcp-editor-title", classes="editor-title", markup=False)
            yield Static("", id="mcp-editor-scope", classes="scope-note", markup=False)
            yield from field("服务器名称", Input(id="mcp-name"))
            yield from field("连接类型", Select((("stdio", "stdio"), ("HTTP", "http")), value="stdio", allow_blank=False, id="mcp-type"))
            yield Checkbox("启用", value=True, id="mcp-enabled")
            yield Static("启动命令", id="mcp-target-label", classes="field-label")
            yield Input(id="mcp-target")
            with Collapsible(title="高级：参数与环境变量 / 请求头", collapsed=True, id="mcp-advanced"):
                with Vertical(id="mcp-args-field", classes="field"):
                    yield Static("Args（YAML 字符串数组）", classes="field-label")
                    yield Input(value="[]", id="mcp-args")
                yield from field("Env / Headers（YAML 字符串对象）", Input(value="{}", id="mcp-map"))
            yield Button("删除服务器", id="mcp-delete", classes="danger-button")

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
        self.query_one("#mcp-editor-scope", Static).update(("全局" if self._mcp_scope == "global" else "当前项目") + " · 保存后重新加载连接")
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
        self.begin_form("#mcp-name")

    @on(Select.Changed, "#mcp-type")
    def mcp_kind_changed(self) -> None:
        self._sync_mcp_kind()

    def _sync_mcp_kind(self) -> None:
        stdio = self.query_one("#mcp-type", Select).value == "stdio"
        self.query_one("#mcp-target-label", Static).update("启动命令" if stdio else "服务器 URL")
        self.query_one("#mcp-args-field").display = stdio

    def save(self, delete: bool = False) -> None:
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
        self.show_catalog()
        self.post_message(DomainSaved("mcp", "MCP 已保存，等待应用。" if changed else "MCP 设置未改变。", restart_required=changed))

    @on(Button.Pressed, "#mcp-delete")
    def delete_mcp(self) -> None:
        self.guard(lambda: self.commit(lambda: self.save(delete=True)))
