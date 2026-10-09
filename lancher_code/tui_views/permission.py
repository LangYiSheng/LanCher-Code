from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Message
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from lancher_code.models import PermissionRequest, PermissionResolution, PermissionResolutionOutcome
from lancher_code.tui_views.chat_controls import ChatAction, ReadOnlyDetailsScreen
from lancher_code.tui_views.theme import theme_palette


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
        colors = theme_palette(self.app.theme)
        foreground = colors["background"] if self._active else colors["text"]
        text = Text(no_wrap=self.has_class("-compact"), overflow="ellipsis")
        text.append("› " if self._active else "  ", style="bold " + foreground if self._active else "")
        label = ("允许本次" if self.outcome == "allow_once" else "拒绝") if self.has_class("-compact") else f"{self.index}. {self.label}"
        text.append(label, style="bold " + foreground if self._active else foreground)
        if self.rule:
            text.append("    ")
            text.append(self.rule, style=foreground if self._active else colors["muted"])
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(PermissionOptionChosen(self.outcome))


class InlinePermissionPanel(VerticalScroll):
    can_focus = True

    DEFAULT_CSS = """
    #permission-body, #permission-primary, #permission-secondary { height: auto; }
    #permission-compact-actions { display: none; height: 1; }
    #permission-compact-actions Button { padding: 0; width: 1fr; min-width: 4; }
    """

    BINDINGS = [
        Binding("up", "previous_option", "上一个", show=False, priority=True),
        Binding("shift+tab", "previous_option", "上一个", show=False, priority=True),
        Binding("down", "next_option", "下一个", show=False, priority=True),
        Binding("tab", "next_option", "下一个", show=False, priority=True),
        Binding("enter", "confirm_option", "确认", show=False, priority=True),
        Binding("escape", "deny_request", "拒绝", show=False, priority=True),
        Binding("ctrl+i", "edit_draft", "编辑草稿", show=False, priority=True),
        Binding("i", "edit_draft", "编辑草稿", show=False, priority=True),
        Binding("m", "toggle_scopes", "更多授权范围", show=False, priority=True),
        Binding("d", "show_details", "查看完整请求", show=False, priority=True),
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
        self._scopes_expanded = False
        self._request_screen: ModalScreen | None = None

    def compose(self) -> ComposeResult:
        yield Static(f"需要确认 · {self.request.tool_label}", id="permission-title", markup=False)
        with Vertical(id="permission-body"):
            if self.request.kind == "command":
                yield Static(self.request.command or "", id="permission-command", markup=False)
                yield Static(self.request.description or "执行此命令需要你的确认", id="permission-description", markup=False)
            else:
                yield Static(self.request.details, id="permission-details", markup=False)
                for preview in self.request.preview_lines:
                    tone = preview.get("tone", "")
                    classes = "permission-preview"
                    if tone in {"error", "success"}:
                        classes += f" -{tone}"
                    yield Static(preview.get("text", ""), classes=classes, markup=False)
            if self.request.metadata.get("cwd"):
                yield Static(f"工作目录：{self.request.metadata['cwd']}", id="permission-cwd", markup=False)
        yield Static(self.request.prompt, id="permission-prompt", markup=False)
        specs = _option_specs(self.request)
        with Vertical(id="permission-primary"):
            for index, (outcome, label, rule) in enumerate(specs[:2], start=1):
                yield PermissionOption(index, outcome, label, rule)
        with Vertical(id="permission-secondary"):
            for index, (outcome, label, rule) in enumerate(specs[2:], start=3):
                option = PermissionOption(index, outcome, label, rule)
                option.display = False
                yield option
        if self.request.kind in {"command", "external_tool"}:
            yield Button("更多授权范围 (M)", id="permission-more", classes="quiet-action")
        yield Static("↑↓ 选择 · Enter 确认 · Esc 拒绝 · D 详情 · M 范围 · Ctrl+I 草稿", id="permission-help")
        with Horizontal(id="permission-compact-actions"):
            yield Button("D 详情", id="permission-show-details", classes="quiet-action")
            if len(specs) > 2:
                yield Button("M 范围", id="permission-show-scopes", classes="quiet-action")
            yield Button("I 输入", id="permission-edit-draft", classes="quiet-action")

    def set_compact(self, compact: bool) -> None:
        self.set_class(compact, "-compact")
        for selector in ("#permission-body", "#permission-prompt", "#permission-help", "#permission-secondary", "#permission-more"):
            for widget in self.query(selector):
                widget.display = not compact
        self.query_one("#permission-compact-actions").display = compact
        for button in self.query("#permission-compact-actions Button"):
            button.styles.width = "1fr"
            button.styles.min_width = 1
            button.styles.padding = 0
        primary = self.query_one("#permission-primary")
        primary.styles.layout = "horizontal" if compact else "vertical"
        primary.styles.height = 1 if compact else "auto"
        for option in self.query("#permission-primary .permission-option"):
            option.set_class(compact, "-compact")
            option.styles.height = 1 if compact else "auto"
            option.styles.padding = (0, 0 if compact else 1)
        if compact:
            for option in self.query("#permission-secondary .permission-option"):
                option.display = False
            self._scopes_expanded = False
            self._selected_index = min(self._selected_index, 1)
        title = f"确认 · {self.request.tool_label} · {self.request.command or self.request.title}" if compact else self.request.title
        title_widget = self.query_one("#permission-title", Static)
        title_widget.styles.height = 1 if compact else "auto"
        title_widget.update(Text(title, no_wrap=True, overflow="ellipsis"))
        if compact:
            self.scroll_home(animate=False)

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

    def action_edit_draft(self) -> None:
        self.post_message(ChatAction("focus-composer"))

    def action_show_details(self) -> None:
        request = self.request
        lines = [request.title, request.command or request.details, request.description or ""]
        if request.metadata.get("cwd"):
            lines.append(f"工作目录：{request.metadata['cwd']}")
        lines.extend(preview.get("text", "") for preview in request.preview_lines)
        self._request_screen = PermissionDetailsScreen("\n".join(line for line in lines if line))
        self.app.push_screen(self._request_screen)

    async def close_request_screen(self) -> None:
        screen = self._request_screen
        self._request_screen = None
        if screen is not None and screen in self.app.screen_stack:
            await screen.dismiss(None)

    @on(Button.Pressed, "#permission-compact-actions Button")
    def handle_compact_action(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "permission-show-details":
            self.action_show_details()
        elif event.button.id == "permission-show-scopes":
            self.action_toggle_scopes()
        else:
            self.action_edit_draft()

    @on(Button.Pressed, "#permission-more")
    def toggle_more(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_toggle_scopes()

    def action_toggle_scopes(self) -> None:
        if self.has_class("-compact"):
            if self.request.kind in {"command", "external_tool"}:
                self._request_screen = PermissionScopesScreen(self.request)
                self.app.push_screen(self._request_screen, self._scope_chosen)
            return
        self._scopes_expanded = not self._scopes_expanded
        for option in self.query(PermissionOption):
            if option.outcome in {"allow_session", "allow_project"}:
                option.display = self._scopes_expanded
        buttons = list(self.query("#permission-more"))
        if buttons:
            buttons[0].label = "收起更多范围 (M)" if self._scopes_expanded else "更多授权范围 (M)"
        self._selected_index = min(self._selected_index, len(self._options()) - 1)
        self._refresh_selection()

    def _scope_chosen(self, outcome: PermissionResolutionOutcome | None) -> None:
        if outcome:
            self._resolve(outcome)
        else:
            self.focus()

    def _move_selection(self, direction: int) -> None:
        options = self._options()
        if not options:
            return
        self._selected_index = (self._selected_index + direction) % len(options)
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        for index, option in enumerate(self._options()):
            option.set_active(index == self._selected_index)
            if index == self._selected_index:
                option.scroll_visible(animate=False)

    def _resolve(self, outcome: PermissionResolutionOutcome) -> None:
        if self._resolved:
            return
        self._resolved = True
        self.post_message(
            self.Resolved(PermissionResolution(request_id=self.request.request_id, outcome=outcome))
        )

    def _options(self) -> list[PermissionOption]:
        return [option for option in self.query(PermissionOption) if option.display]


class PermissionDetailsScreen(ReadOnlyDetailsScreen):
    BINDINGS = [Binding("ctrl+c", "cancel_task", "停止任务", show=False, priority=True)]

    async def action_cancel_task(self) -> None:
        await self.app.action_request_quit()


class PermissionScopesScreen(ModalScreen[PermissionResolutionOutcome | None]):
    """小屏把较大授权范围放在独立页面，让精确规则可读后再确认。"""

    DEFAULT_CSS = """
    PermissionScopesScreen { align: center middle; background: $background 70%; }
    #permission-scopes-dialog { width: 72; max-width: 96%; height: auto; max-height: 90%; background: $surface; padding: 1; }
    #permission-scopes-dialog Static { height: auto; margin-bottom: 1; }
    #permission-scopes-dialog Button { width: 1fr; min-width: 8; height: 3; background: transparent; border: none; color: $text; text-style: none; }
    #permission-scopes-dialog Button:focus { background: $foreground; color: $background; text-style: bold; }
    """
    BINDINGS = [
        Binding("escape", "close", "返回"),
        Binding("ctrl+c", "cancel_task", "停止任务", show=False, priority=True),
    ]

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="permission-scopes-dialog"):
            yield Static("扩大授权范围", markup=False)
            yield Static("后续匹配此规则的操作将不再逐次询问。", markup=False)
            yield Static(f"本次会话规则：\n{self.request.session_rule or '此操作'}", id="scope-session-rule", markup=False)
            yield Button("仅此会话放行", id="scope-session")
            yield Static(f"当前项目规则：\n{self.request.project_rule or '此操作'}", id="scope-project-rule", markup=False)
            yield Button("保存到当前项目", id="scope-project")
            yield Button("返回", id="scope-back")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        outcome = {"scope-session": "allow_session", "scope-project": "allow_project"}.get(event.button.id)
        self.dismiss(outcome)

    def action_close(self) -> None:
        self.dismiss(None)

    async def action_cancel_task(self) -> None:
        await self.app.action_request_quit()


def _option_specs(
    request: PermissionRequest,
) -> list[tuple[PermissionResolutionOutcome, str, str | None]]:
    if request.kind == "command":
        return [
            ("allow_once", "仅允许本次", None),
            ("deny", "拒绝执行", None),
            ("allow_session", "在本次会话中放行", request.session_rule),
            ("allow_project", "在当前项目中放行", request.project_rule),
        ]
    if request.kind == "external_tool":
        return [
            ("allow_once", "仅允许本次", None),
            ("deny", "拒绝调用", None),
            ("allow_session", "在本次会话中放行", request.session_rule),
            ("allow_project", "在当前项目中放行", request.project_rule),
        ]
    return [
        ("allow_once", "仅允许本次", None),
        ("deny", "拒绝本次编辑", None),
    ]
