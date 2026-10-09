"""命令触发的设置提交和明确目标的确认界面。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from lancher_code.model_catalog import iter_model_refs

if TYPE_CHECKING:
    from lancher_code.tui_views.chat import LanCherTextualApp


def save_command_setting(app: LanCherTextualApp, key: str, value: str) -> None:
    service = app._settings_service
    if service is None:
        raise ValueError("设置服务尚未加载。")
    snapshot = service.load()
    if key == "default-model":
        if value not in iter_model_refs(snapshot.config):
            raise ValueError("模型不存在，请选择有效的供应商 ID/模型 ID。")
        snapshot.config.default_model = value
        saved = service.save_models(snapshot.config)
        try:
            app._apply_models_settings(saved)
        except Exception as exc:
            # 文件已保存，运行时失败必须单独报告；不能假装全部失败。
            app.notify(f"默认模型已保存；运行时刷新失败：{exc}", severity="warning", timeout=10)
        else:
            app.notify("新对话默认模型已保存；本次模型保持不变。", title="设置")
        return
    ui = snapshot.config.ui
    if key == "theme":
        ui.theme = value
    elif key == "thinking":
        ui.show_thinking_status = value == "on"
    elif key == "busy-enter":
        ui.busy_enter_action = value
    else:
        raise ValueError("未知设置项。")
    saved = service.save_ui(ui)
    try:
        app._apply_ui_settings(saved.ui)
    except Exception as exc:
        app.notify(f"设置已保存；界面刷新失败：{exc}", severity="warning", timeout=10)
    else:
        app.notify("设置已保存并生效。", title="设置")


class CommandConfirmationScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "返回编辑")]
    CSS = """
    CommandConfirmationScreen { align: center middle; background: $background 70%; }
    #command-confirm-box { width: 72; max-width: 96%; height: auto; max-height: 90%; padding: 1; background: $surface; }
    #command-confirm-copy { height: auto; max-height: 12; }
    #command-confirm-copy Static { height: auto; }
    #command-confirm-title { text-style: bold; color: $warning; margin-bottom: 1; }
    #command-confirm-actions { height: 3; }
    #command-confirm-actions Button { width: 1fr; min-width: 6; border: none; background: transparent; color: $text; }
    #command-confirm-actions Button:focus { background: $foreground; color: $background; text-style: bold; }
    """

    def __init__(self, description: str, command: str) -> None:
        super().__init__()
        self.description, self.command = description, command

    def compose(self) -> ComposeResult:
        with Vertical(id="command-confirm-box"):
            with VerticalScroll(id="command-confirm-copy"):
                yield Static("确认此操作", id="command-confirm-title")
                yield Static(self.description, markup=False)
                yield Static(self.command, markup=False)
            with Horizontal(id="command-confirm-actions"):
                yield Button("返回编辑", id="command-cancel")
                yield Button("确认执行", id="command-confirm")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "command-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
