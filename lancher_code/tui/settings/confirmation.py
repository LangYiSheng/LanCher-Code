from __future__ import annotations
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

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
