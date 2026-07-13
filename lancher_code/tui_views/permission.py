from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Click, Message
from textual.widgets import Static

from lancher_code.models import PermissionRequest, PermissionResolution, PermissionResolutionOutcome


class PermissionOptionChosen(Message):
    def __init__(self, outcome: PermissionResolutionOutcome) -> None:
        super().__init__()
        self.outcome = outcome


class PermissionOption(Static):
    def __init__(
        self,
        index: int,
        outcome: PermissionResolutionOutcome,
        label: str,
        rule: str | None = None,
    ) -> None:
        super().__init__(classes="permission-option")
        self.index = index
        self.outcome = outcome
        self.label = label
        self.rule = rule
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active
        self.set_class(active, "-active")
        self.refresh()

    def render(self) -> RenderableType:
        text = Text()
        text.append("> " if self._active else "  ", style="bold #73b6ff" if self._active else "")
        text.append(f"{self.index}. {self.label}", style="bold #f2f2f2" if self._active else "#c8d5e3")
        if self.rule:
            text.append("    ")
            text.append(self.rule, style="#a8b9cc")
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(PermissionOptionChosen(self.outcome))


class InlinePermissionPanel(Vertical):
    can_focus = True

    BINDINGS = [
        Binding("up", "previous_option", "上一个", show=False, priority=True),
        Binding("shift+tab", "previous_option", "上一个", show=False, priority=True),
        Binding("down", "next_option", "下一个", show=False, priority=True),
        Binding("tab", "next_option", "下一个", show=False, priority=True),
        Binding("enter", "confirm_option", "确认", show=False, priority=True),
        Binding("escape", "deny_request", "拒绝", show=False, priority=True),
    ]

    class Resolved(Message):
        def __init__(self, resolution: PermissionResolution) -> None:
            super().__init__()
            self.resolution = resolution

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__(id="inline-permission-panel")
        self.request = request
        self._selected_index = 0
        self._resolved = False

    def compose(self) -> ComposeResult:
        if self.request.kind == "command":
            yield Static(f"{self.request.tool_label} 命令", id="permission-title")
            yield Static(self.request.command or "", id="permission-command")
            yield Static(self.request.description or "(无描述)", id="permission-description")
        else:
            yield Static(self.request.title, id="permission-title")
            yield Static(self.request.details, id="permission-details")
            for preview in self.request.preview_lines:
                tone = preview.get("tone", "")
                classes = "permission-preview"
                if tone in {"error", "success"}:
                    classes += f" -{tone}"
                yield Static(preview.get("text", ""), classes=classes)
        yield Static(self.request.prompt, id="permission-prompt")
        for index, (outcome, label, rule) in enumerate(_option_specs(self.request), start=1):
            yield PermissionOption(index, outcome, label, rule)
        yield Static("↑/↓ 选择   Enter 确认   Esc 拒绝", id="permission-help")

    def on_mount(self) -> None:
        self._refresh_selection()
        self.focus()

    @on(PermissionOptionChosen)
    def handle_option_chosen(self, event: PermissionOptionChosen) -> None:
        event.stop()
        options = self._options()
        for index, option in enumerate(options):
            if option.outcome == event.outcome:
                self._selected_index = index
                break
        self._refresh_selection()
        self._resolve(event.outcome)

    def action_previous_option(self) -> None:
        self._move_selection(-1)

    def action_next_option(self) -> None:
        self._move_selection(1)

    def action_confirm_option(self) -> None:
        options = self._options()
        if options:
            self._resolve(options[self._selected_index].outcome)

    def action_deny_request(self) -> None:
        self._resolve("deny")

    def _move_selection(self, direction: int) -> None:
        options = self._options()
        if not options:
            return
        self._selected_index = (self._selected_index + direction) % len(options)
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        for index, option in enumerate(self._options()):
            option.set_active(index == self._selected_index)

    def _resolve(self, outcome: PermissionResolutionOutcome) -> None:
        if self._resolved:
            return
        self._resolved = True
        self.post_message(
            self.Resolved(PermissionResolution(request_id=self.request.request_id, outcome=outcome))
        )

    def _options(self) -> list[PermissionOption]:
        return list(self.query(PermissionOption))


def _option_specs(
    request: PermissionRequest,
) -> list[tuple[PermissionResolutionOutcome, str, str | None]]:
    if request.kind == "command":
        return [
            ("allow_once", "仅允许执行本次命令", None),
            ("allow_session", "在本次会话中放行", request.session_rule),
            ("allow_project", "在当前项目中放行", request.project_rule),
            ("deny", "拒绝执行", None),
        ]
    return [
        ("allow_once", "仅允许本次编辑", None),
        ("deny", "拒绝本次编辑", None),
    ]
