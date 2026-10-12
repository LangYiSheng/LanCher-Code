"""系统实验选项的表单，协议能力判断和运行时应用属于智能体核心。"""
from __future__ import annotations

from copy import deepcopy

from textual.app import ComposeResult
from textual.widgets import Checkbox, Input, Static

from lancher_code.config.models import RuntimeConfig
from lancher_code.config.settings import SettingsError, SettingsService
from lancher_code.tui.settings.common import DomainSaved, SettingsDomainEditor, field


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
        self.query_one("#runtime-output-continuations", Input).value = str(self.snapshot.config.runtime.output_continuation_limit)
        self._baseline = self.values()

    def collect(self) -> RuntimeConfig:
        runtime = deepcopy(self.snapshot.config.runtime)
        runtime.experimental_mcp_tool_append = self.query_one("#runtime-mcp-tool-append", Checkbox).value
        raw = self.query_one("#runtime-output-continuations", Input).value.strip()
        if not raw.isdecimal() or int(raw) < 1:
            raise SettingsError("输出自动续接次数必须是正整数。", field_id="runtime-output-continuations")
        runtime.output_continuation_limit = int(raw)
        return runtime

    def save(self, runtime: RuntimeConfig | None = None) -> None:
        runtime = runtime or self.collect()
        self.snapshot.config = self.service.save_runtime(runtime)
        self.show_catalog()
        self.post_message(DomainSaved("runtime", "系统设置已保存，重启后生效。", runtime=deepcopy(runtime)))

    def compose(self) -> ComposeResult:
        yield Static("系统设置", classes="editor-title")
        yield from field("输出达到额度后的自动续接次数", Input("3", type="integer", id="runtime-output-continuations"))
        yield Static("达到单次输出额度时保留内容并自动继续；连续耗尽达到此次数后明确报告未完成，避免无限重复生成。",
                     classes="scope-note")
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
