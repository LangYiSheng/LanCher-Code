"""外观与输入偏好的草稿和提交。"""
from __future__ import annotations
from copy import deepcopy
from textual.app import ComposeResult
from textual.widgets import Select, Static
from lancher_code.config.settings import SettingsService
from lancher_code.tui.settings.common import SettingsDomainEditor, DomainSaved, field

class AppearanceSettingsEditor(SettingsDomainEditor):
    domain = "ui"
    editor_selector = ""

    def __init__(self, service: SettingsService) -> None:
        super().__init__(service, id="page-ui")

    def show_catalog(self) -> None:
        if self.snapshot is None:
            return
        self.editing = True
        with self.prevent(Select.Changed):
            self.query_one("#ui-theme", Select).value = self.snapshot.config.ui.theme
            self.query_one("#ui-busy-enter", Select).value = self.snapshot.config.ui.busy_enter_action
        self._baseline = self.values()

    def save(self) -> None:
        ui = deepcopy(self.snapshot.config.ui)
        ui.theme = self.query_one("#ui-theme", Select).value
        ui.busy_enter_action = self.query_one("#ui-busy-enter", Select).value
        self.snapshot.config = self.service.save_ui(ui)
        self.show_catalog()
        self.post_message(DomainSaved("ui", "外观与输入偏好已保存。", ui=ui))

    def compose(self) -> ComposeResult:
        yield Static("外观与忙时输入", classes="editor-title")
        yield from field("终端外观", Select((("深色", "dark"), ("浅色", "light")), value="dark", allow_blank=False, id="ui-theme"))
        yield from field("工作中按 Enter", Select((("排到下一轮", "follow_up"), ("补充当前任务", "steer"), ("仅保留草稿", "draft")), value="follow_up", allow_blank=False, id="ui-busy-enter"))
        yield Static("只影响工作中的输入；闲置时 Enter 仍发送。保存后生效。", classes="scope-note")
