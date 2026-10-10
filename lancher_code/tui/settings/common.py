"""设置编辑器共享的表单状态与提交消息。"""
from __future__ import annotations
from collections.abc import Callable
from typing import Any
import yaml
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static
from lancher_code.config.models import RuntimeConfig, UIConfig
from lancher_code.config.settings import SettingsError, SettingsService, SettingsSnapshot
from lancher_code.tui.settings.confirmation import DiscardChangesScreen

def field(label: str, widget) -> ComposeResult:
    with Vertical(classes="field"):
        yield Static(label, classes="field-label")
        yield widget

class EditorViewChanged(Message):
    pass

class DomainError(Message):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

class DomainSaved(Message):
    def __init__(self, domain: str, notice: str, *, restart_required: bool = False,
                 ui: UIConfig | None = None, runtime: RuntimeConfig | None = None) -> None:
        super().__init__()
        self.domain, self.notice = domain, notice
        self.restart_required, self.ui = restart_required, ui
        self.runtime = runtime

class SettingsDomainEditor(Vertical):
    domain: str
    editor_selector: str

    def __init__(self, service: SettingsService, *, id: str) -> None:
        super().__init__(id=id, classes="settings-page")
        self.service = service
        self.snapshot: SettingsSnapshot | None = None
        self.editing = False
        self._baseline: dict[str, Any] = {}

    def load(self, snapshot: SettingsSnapshot) -> None:
        self.snapshot = snapshot
        self.show_catalog()

    def values(self) -> dict[str, Any]:
        root = self.query_one(self.editor_selector) if self.editor_selector else self
        return {widget.id: widget.value for widget in root.query("Input, Select, Checkbox")}

    @property
    def dirty(self) -> bool:
        return self.editing and self.values() != self._baseline

    def begin_form(self, focus_selector: str) -> None:
        self.editing = True
        self._baseline = self.values()
        self.post_message(EditorViewChanged())
        self.query_one(focus_selector).focus()

    def guard(self, action: Callable[[], None]) -> None:
        if self.dirty:
            self.app.push_screen(DiscardChangesScreen(), lambda discard: action() if discard else None)
        else:
            action()

    def commit(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except (SettingsError, ValueError, yaml.YAMLError) as exc:
            self.post_message(DomainError(str(exc)))

    def show_catalog(self) -> None:
        raise NotImplementedError

    def save(self) -> None:
        raise NotImplementedError
