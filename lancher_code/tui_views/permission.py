from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from lancher_code.models import PermissionRequest, PermissionResolution, PermissionResolutionOutcome


class PermissionRequestScreen(ModalScreen[PermissionResolution]):
    CSS = """
    PermissionRequestScreen {
        align: center middle;
    }

    #permission-dialog {
        width: 80;
        max-width: 90%;
        height: auto;
        border: round #4b6f97;
        background: #0f1a26;
        padding: 1 2;
    }

    #permission-title {
        color: #73b6ff;
        text-style: bold;
        margin-bottom: 1;
    }

    .permission-section {
        margin-bottom: 1;
        height: auto;
    }

    .permission-preview {
        color: #c8d5e3;
    }

    .permission-preview.-error {
        color: #ff7b72;
    }

    .permission-preview.-success {
        color: #78d98a;
    }

    #permission-help {
        margin-top: 1;
        color: #97adc7;
    }

    #permission-actions {
        margin-top: 1;
        height: auto;
    }

    #permission-actions Button {
        margin-right: 1;
    }

    #permission-actions Button:focus {
        border: heavy #73b6ff;
    }
    """

    BINDINGS = [
        Binding("tab", "focus_next_option", "下一个", show=False, priority=True),
        Binding("shift+tab", "focus_previous_option", "上一个", show=False, priority=True),
        Binding("right", "focus_next_option", "下一个", show=False, priority=True),
        Binding("down", "focus_next_option", "下一个", show=False, priority=True),
        Binding("left", "focus_previous_option", "上一个", show=False, priority=True),
        Binding("up", "focus_previous_option", "上一个", show=False, priority=True),
        Binding("enter", "confirm_focused_option", "确认", show=False, priority=True),
        Binding("escape", "deny_request", "拒绝", show=False, priority=True),
    ]

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-dialog"):
            yield Static(self.request.title, id="permission-title")
            yield Static(self.request.prompt, classes="permission-section")
            yield Static(self.request.details, classes="permission-section")
            for preview in self.request.preview_lines:
                tone = preview.get("tone", "")
                css_class = "permission-preview"
                if tone == "error":
                    css_class += " -error"
                elif tone == "success":
                    css_class += " -success"
                yield Static(preview.get("text", ""), classes=css_class)
            with Horizontal(id="permission-actions"):
                for outcome, label in _button_specs(self.request):
                    yield Button(label, id=f"permission-{outcome}")
            yield Static("Tab / Shift+Tab 切换选项，Enter 确认，Esc 拒绝。", id="permission-help")

    def on_mount(self) -> None:
        first_button = next(iter(self._buttons()), None)
        if first_button is not None:
            first_button.focus()

    @on(Button.Pressed)
    def handle_button_pressed(self, event: Button.Pressed) -> None:
        self._dismiss_with_outcome(_outcome_from_button_id(event.button.id))

    def action_focus_next_option(self) -> None:
        self._move_focus(1)

    def action_focus_previous_option(self) -> None:
        self._move_focus(-1)

    def action_confirm_focused_option(self) -> None:
        focused = self.focused
        if isinstance(focused, Button):
            self._dismiss_with_outcome(_outcome_from_button_id(focused.id))

    def action_deny_request(self) -> None:
        self._dismiss_with_outcome("deny")

    def _move_focus(self, direction: int) -> None:
        buttons = self._buttons()
        if not buttons:
            return

        focused = self.focused
        if focused not in buttons:
            buttons[0].focus()
            return

        index = buttons.index(focused)
        buttons[(index + direction) % len(buttons)].focus()

    def _dismiss_with_outcome(self, outcome: PermissionResolutionOutcome) -> None:
        self.dismiss(PermissionResolution(request_id=self.request.request_id, outcome=outcome))

    def _buttons(self) -> list[Button]:
        return list(self.query(Button))


def _outcome_from_button_id(button_id: str | None) -> PermissionResolutionOutcome:
    outcome = button_id.removeprefix("permission-") if button_id else "deny"
    if outcome in {"allow_once", "allow_session", "allow_project", "deny"}:
        return outcome
    return "deny"


def _button_specs(request: PermissionRequest) -> list[tuple[PermissionResolutionOutcome, str]]:
    if request.kind == "command":
        return [
            ("allow_once", "允许执行本次命令"),
            ("allow_session", "在本会话中永久放行"),
            ("allow_project", "在本项目中永久放行"),
            ("deny", "拒绝执行"),
        ]
    return [
        ("allow_once", "允许本次编辑"),
        ("deny", "拒绝本次编辑"),
    ]
