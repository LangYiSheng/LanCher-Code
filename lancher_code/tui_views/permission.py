from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual import on
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

    #permission-actions {
        margin-top: 1;
        height: auto;
    }

    #permission-actions Button {
        margin-right: 1;
    }
    """

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

    @on(Button.Pressed)
    def handle_button_pressed(self, event: Button.Pressed) -> None:
        outcome = event.button.id.removeprefix("permission-") if event.button.id else "deny"
        self.dismiss(PermissionResolution(request_id=self.request.request_id, outcome=outcome))  # type: ignore[arg-type]


def _button_specs(request: PermissionRequest) -> list[tuple[PermissionResolutionOutcome, str]]:
    if request.kind == "command":
        return [
            ("allow_once", "允许执行本次命令"),
            ("allow_session", "本会话永久放行"),
            ("allow_project", "本项目永久放行"),
            ("deny", "拒绝执行"),
        ]
    return [
        ("allow_once", "允许本次编辑"),
        ("deny", "拒绝本次编辑"),
    ]
