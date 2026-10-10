"""系统实验选项的表单，协议能力判断和运行时应用属于智能体核心。"""
from __future__ import annotations

from copy import deepcopy

from textual.app import ComposeResult
from textual.widgets import Checkbox, Static

from lancher_code.config.models import RuntimeConfig
from lancher_code.config.settings import SettingsService
from lancher_code.tui.settings.common import DomainSaved, SettingsDomainEditor


class SystemSettingsEditor(SettingsDomainEditor):
    domain = "runtime"
    editor_selector = ""

    def __init__(self, service: SettingsService) -> None:
        super().__init__(service, id="page-runtime")

    def show_catalog(self) -> None:
        if self.snapshot is None:
            return
        self.editing = True
        with self.prevent(Checkbox.Changed):
            self.query_one("#runtime-mcp-tool-append", Checkbox).value = self.snapshot.config.runtime.experimental_mcp_tool_append
        self._baseline = self.values()

    def collect(self) -> RuntimeConfig:
        runtime = deepcopy(self.snapshot.config.runtime)
        runtime.experimental_mcp_tool_append = self.query_one("#runtime-mcp-tool-append", Checkbox).value
        return runtime

    def save(self, runtime: RuntimeConfig | None = None) -> None:
        runtime = runtime or self.collect()
        self.snapshot.config = self.service.save_runtime(runtime)
        self.show_catalog()
        self.post_message(DomainSaved("runtime", "系统设置已保存，重启后生效。", runtime=deepcopy(runtime)))

    def compose(self) -> ComposeResult:
        yield Static("系统设置 · 实验功能", classes="editor-title")
        yield Checkbox("原生 MCP 工具追加", value=False, id="runtime-mcp-tool-append")
        yield Static(
            "默认关闭。只适合明确支持原生 MCP 工具追加的模型端点。"
            "开启前请确认供应商兼容；不支持时保持关闭。",
            classes="scope-note", id="runtime-mcp-compatibility",
        )
        yield Static(
            "关闭时使用常规工具定义；开启后采用实验请求方式。"
            "保存由核心应用，不改变阶段、审批策略或外部连接权限。",
            classes="scope-note", id="runtime-mcp-scope",
        )
