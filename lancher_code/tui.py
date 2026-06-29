from __future__ import annotations

from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Message
from textual.widgets import Static, TextArea

from lancher_code.models import ProviderConfig, SessionMessage, TraceEntry, TurnEvent, UIConfig
from lancher_code.session import SessionController
from lancher_code.slash_commands import (
    SlashCommandDefinition,
    SlashCommandRegistry,
    create_default_slash_command_registry,
    extract_exact_command_name,
    extract_slash_menu_query,
)
from lancher_code.turn_runner import TurnRunner

BANNER_TEXT = r"""
    __                ________                 ______          __
   / /   ____ _____  / ____/ /_  ___  _____   / ____/___  ____/ /__
  / /   / __ `/ __ \/ /   / __ \/ _ \/ ___/  / /   / __ \/ __  / _ \
 / /___/ /_/ / / / / /___/ / / /  __/ /     / /___/ /_/ / /_/ /  __/
/_____/\__,_/_/ /_/\____/_/ /_/\___/_/      \____/\____/\__,_/\___/
"""

MIN_COMPOSER_LINES = 1
MAX_COMPOSER_LINES = 6
COMPOSER_FRAME_HEIGHT = 2
DEFAULT_COMMAND_HINT = ""
NORMAL_PLACEHOLDER = "发送一条消息"
PLAN_PLACEHOLDER = "Plan Mode: 继续补充或修改计划"


class ComposerSubmitted(Message):
    def __init__(self, composer: "ComposerTextArea", value: str) -> None:
        super().__init__()
        self.composer = composer
        self.value = value


class SlashMenuNavigateRequested(Message):
    def __init__(self, direction: int) -> None:
        super().__init__()
        self.direction = direction


class SlashMenuAcceptRequested(Message):
    pass


class SlashMenuDismissRequested(Message):
    pass


class SlashCommandChosen(Message):
    def __init__(self, command_name: str) -> None:
        super().__init__()
        self.command_name = command_name


class ComposerTextArea(TextArea):
    BINDINGS = [
        Binding("enter", "submit_message", "发送", show=False, priority=True),
        Binding("tab", "accept_slash_menu_selection", "补全命令", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "换行", show=False, priority=True),
    ] + TextArea.BINDINGS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.slash_menu_active = False
        self._accepted_slash_command_text: str | None = None

    def _on_key(self, event: events.Key) -> None:
        if self.slash_menu_active:
            if event.key == "up":
                self.post_message(SlashMenuNavigateRequested(-1))
                event.prevent_default()
                return
            if event.key == "down":
                self.post_message(SlashMenuNavigateRequested(1))
                event.prevent_default()
                return
            if event.key == "tab":
                self.post_message(SlashMenuAcceptRequested())
                event.prevent_default()
                return
            if event.key == "escape":
                self.post_message(SlashMenuDismissRequested())
                event.prevent_default()
                return
        super()._on_key(event)

    def action_submit_message(self) -> None:
        if self.slash_menu_active:
            self.post_message(SlashMenuAcceptRequested())
            return
        self.post_message(ComposerSubmitted(self, self.text))

    def action_accept_slash_menu_selection(self) -> None:
        if self.slash_menu_active:
            self.post_message(SlashMenuAcceptRequested())

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def remember_accepted_slash_command(self, command_text: str) -> None:
        self._accepted_slash_command_text = command_text

    def should_suppress_slash_menu(self) -> bool:
        return self._accepted_slash_command_text == self.text

    def clear_accepted_slash_command_if_needed(self) -> None:
        if self._accepted_slash_command_text != self.text:
            self._accepted_slash_command_text = None


class SlashCommandMenuItem(Static):
    def __init__(self, definition: SlashCommandDefinition) -> None:
        super().__init__(classes="slash-command-item")
        self.definition = definition
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active
        self.set_class(active, "-active")
        self.refresh()

    def render(self) -> RenderableType:
        text = Text()
        text.append(self.definition.usage, style="bold #73b6ff" if not self._active else "bold #f2f2f2")
        text.append("  ")
        text.append(self.definition.description, style="#a8b9cc" if not self._active else "#dbe7f3")
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(SlashCommandChosen(self.definition.name))


class SlashCommandMenu(Vertical):
    def __init__(self, commands: list[SlashCommandDefinition]) -> None:
        super().__init__(id="slash-command-menu")
        self._commands = commands

    def compose(self) -> ComposeResult:
        for command in self._commands:
            yield SlashCommandMenuItem(command)

    def set_matches(self, commands: list[SlashCommandDefinition], active_name: str | None) -> None:
        visible_names = {command.name for command in commands}
        self.display = bool(commands)
        for item in self.query(SlashCommandMenuItem):
            visible = item.definition.name in visible_names
            item.display = visible
            item.set_active(visible and item.definition.name == active_name)


class CommandHintBar(Static):
    def __init__(self) -> None:
        super().__init__("", id="command-hint")
        self.display = False

    def set_hint(self, hint: str) -> None:
        self.update(hint)
        self.display = bool(hint)


class BannerWidget(Static):
    def __init__(self, cwd: Path) -> None:
        super().__init__(id="banner")
        self._cwd = cwd
        self._compact = False

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.set_class(compact, "-compact")
        self.refresh()

    def render(self) -> RenderableType:
        if self._compact:
            compact_text = Text()
            compact_text.append("LanCher Code", style="bold #73b6ff")
            compact_text.append("  ")
            compact_text.append("工作目录：", style="bold #73b6ff")
            compact_text.append(str(self._cwd), style="default")
            return compact_text

        title = Text(BANNER_TEXT.strip("\n"), style="bold #73b6ff")
        subtitle = Text()
        subtitle.append("当前工作目录：", style="bold #73b6ff")
        subtitle.append(str(self._cwd), style="default")
        return Group(title, subtitle)


class ThinkingTraceWidget(Vertical):
    def __init__(self, entries: list[TraceEntry], *, collapsed: bool = True) -> None:
        super().__init__(classes="thinking-trace")
        self._entries = list(entries)
        self._collapsed = collapsed

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def compose(self) -> ComposeResult:
        yield Static(classes="thinking-trace-header")
        yield Static(classes="thinking-trace-body")

    def on_mount(self) -> None:
        self._sync_view()

    @on(Click, ".thinking-trace-header")
    def toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self._sync_view()

    def set_collapsed(self, collapsed: bool) -> None:
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._sync_view()

    def update_entries(self, entries: list[TraceEntry]) -> None:
        self._entries = list(entries)
        self._sync_view()

    def _sync_view(self) -> None:
        header = self.query_one(".thinking-trace-header", Static)
        body = self.query_one(".thinking-trace-body", Static)
        marker = "▶" if self._collapsed else "▼"
        header.update(f"{marker} 思考轨迹 ({len(self._entries)})")
        header.styles.color = "#a8b9cc"
        header.styles.text_style = "bold"
        body.display = bool(self._entries) and not self._collapsed
        if body.display:
            body.update(_format_trace_entries(self._entries))


def _format_trace_entries(entries: list[TraceEntry]) -> Text:
    renderable = Text()
    for entry in entries:
        if entry.kind == "thinking":
            renderable.append(entry.text, style="#a8b9cc")
        elif entry.kind == "tool_call":
            renderable.append(_format_tool_call_entry(entry), style="#73b6ff")
        elif entry.kind == "tool_result":
            prefix = "✓ " if entry.ok else "✗ "
            style = "#78d98a" if entry.ok else "#ff7b72"
            renderable.append(f"{prefix}{entry.text}", style=style)
            for display_line in entry.metadata.get("display_lines", []):
                if not isinstance(display_line, dict):
                    continue
                line_text = display_line.get("text")
                if not isinstance(line_text, str):
                    continue
                tone = display_line.get("tone")
                line_style = "#78d98a" if tone == "success" else "#ff7b72" if tone == "error" else style
                renderable.append("\n")
                renderable.append(line_text, style=line_style)
        elif entry.kind == "text":
            renderable.append(entry.text, style="#e8e8e8")
        elif entry.kind == "notice":
            renderable.append(f"提示：{entry.text}", style="#ffb86c")
        renderable.append("\n")

    if renderable.plain.endswith("\n"):
        renderable.rstrip()
    return renderable


def _format_tool_call_entry(entry: TraceEntry) -> str:
    if not entry.arguments:
        return f"● {entry.tool_name}"
    parts: list[str] = []
    for key, value in entry.arguments.items():
        rendered = str(value)
        if len(rendered) > 24:
            rendered = rendered[:24] + "..."
        parts.append(f"{key}={rendered}")
    return f"● {entry.tool_name}({', '.join(parts[:2])})"


class MessageWidget(Vertical):
    ROLE_LABELS = {
        "system": "SYSTEM",
        "user": "YOU",
        "assistant": "LANCHER",
    }
    ROLE_STYLES = {
        "system": "#97adc7",
        "user": "#78d98a",
        "assistant": "#73b6ff",
    }
    STATUS_LABELS = {
        "error": "ERROR",
        "cancelled": "CANCELLED",
    }

    def __init__(self, message: SessionMessage, *, show_thinking: bool) -> None:
        super().__init__(classes=f"message message--{message.role}")
        self.message_id = message.id
        self._show_thinking = show_thinking
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self.trace_collapsed = message.trace.collapsed

    def compose(self) -> ComposeResult:
        yield Static(classes="message-label")
        yield ThinkingTraceWidget(self.trace_entries, collapsed=self.trace_collapsed)
        yield Static(classes="message-body")

    def on_mount(self) -> None:
        self._sync_view()

    def update_from_message(self, message: SessionMessage) -> None:
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self.trace_collapsed = message.trace.collapsed
        self._sync_view()

    def _sync_view(self) -> None:
        self.set_class(self.status in {"error", "cancelled"}, "-error")

        label_widget = self.query_one(".message-label", Static)
        label_widget.update(self._label_text())
        label_widget.styles.color = self._label_color()
        label_widget.styles.text_style = "bold"

        trace_widget = self.query_one(ThinkingTraceWidget)
        trace_visible = self._show_trace()
        trace_widget.display = trace_visible
        if trace_visible:
            trace_widget.set_collapsed(self.trace_collapsed)
            trace_widget.update_entries(self.trace_entries)

        body_widget = self.query_one(".message-body", Static)
        body_text = self._body_text()
        body_widget.display = bool(body_text)
        if body_text:
            body_widget.styles.color = self._body_color()
            body_widget.update(body_text)

    def _show_trace(self) -> bool:
        return self._show_thinking and self.role == "assistant" and bool(self.trace_entries)

    def _label_text(self) -> str:
        if self.status in self.STATUS_LABELS:
            return self.STATUS_LABELS[self.status]
        return self.ROLE_LABELS.get(self.role, self.role.upper())

    def _label_color(self) -> str:
        if self.status in {"error", "cancelled"}:
            return "#ff7b72"
        return self.ROLE_STYLES.get(self.role, "#ffffff")

    def _body_text(self) -> str:
        if self.status == "error":
            return self.content or "请求失败。"
        if self.status == "cancelled":
            return self.content or "本轮已取消。"
        if self.content:
            return self.content
        if self.status == "streaming" and not self.trace_entries:
            return "等待模型回复..."
        if self.status == "complete" and not self.trace_entries:
            return "本轮未收到任何回复。"
        return ""

    def _body_color(self) -> str:
        if self.status in {"error", "cancelled"}:
            return "#ff7b72"
        return "#e8e8e8"


class LanCherTextualApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
        color: #f2f2f2;
    }

    #root {
        height: 100%;
        layout: vertical;
    }

    #banner {
        margin: 0 1 0 1;
        padding: 0 1 0 1;
        border: tall #73b6ff;
        width: 1fr;
    }

    #banner.-compact {
        border: none;
        margin: 0 1 0 1;
        padding: 0;
        height: auto;
    }

    #chat-view {
        margin: 1 1 0 1;
        padding: 1 1;
        border: round #4b6f97;
        height: 1fr;
        width: 1fr;
    }

    .message {
        margin: 0 0 1 0;
        padding: 0 0 0 1;
        width: 100%;
        height: auto;
        layout: vertical;
    }

    .message-label, .message-body, .thinking-trace-header, .thinking-trace-body {
        width: 1fr;
        height: auto;
    }

    .thinking-trace {
        margin: 0 0 1 0;
        width: 1fr;
        height: auto;
        layout: vertical;
    }

    .thinking-trace-body {
        color: #a8b9cc;
    }

    #chat-view.-banner-collapsed {
        margin-top: 0;
    }

    .message--user {
        border-left: wide #78d98a;
    }

    .message--assistant {
        border-left: wide #73b6ff;
    }

    .message--system {
        border-left: wide #97adc7;
    }

    .message.-error {
        border-left: wide #ff7b72;
    }

    #composer-region {
        margin: 1 1 0 1;
        width: 1fr;
        layout: vertical;
        height: auto;
    }

    #slash-command-menu {
        border: round #4b6f97;
        background: #0f1a26;
        padding: 0 0;
        margin: 0 0 1 0;
        display: none;
        width: 1fr;
        height: auto;
    }

    .slash-command-item {
        padding: 0 1;
        width: 1fr;
        color: #c8d5e3;
    }

    .slash-command-item.-active {
        background: #203246;
    }

    #composer {
        border: tall #4b6f97;
        height: 3;
        min-height: 3;
        max-height: 8;
        layout: horizontal;
        width: 1fr;
        padding: 0 1;
    }

    #composer:focus-within {
        border: tall #73b6ff;
    }

    #prompt-glyph {
        width: 2;
        content-align: center middle;
        color: #73b6ff;
        text-style: bold;
    }

    #composer-input {
        width: 1fr;
        height: 100%;
        min-height: 1;
        max-height: 6;
        margin: 0;
        padding: 0;
        background: transparent;
        border: none;
    }

    #composer-input:focus {
        border: none;
        background: transparent;
    }

    #composer-input .text-area--cursor-line {
        background: transparent;
    }

    #composer-input .text-area--placeholder {
        color: #7f9ab8;
    }

    #composer-input .text-area--cursor {
        background: #73b6ff;
        color: black;
    }

    #command-hint {
        margin: 0 0 1 0;
        color: #7f9ab8;
        height: 1;
        width: 1fr;
    }

    #status-bar {
        margin: 0 1 1 1;
        height: 1;
        layout: horizontal;
        color: #97adc7;
        width: 1fr;
    }

    #status-left {
        width: 1fr;
    }

    #status-center {
        width: auto;
        text-align: center;
    }

    #status-right {
        width: 1fr;
        text-align: right;
    }
    """

    BINDINGS = [
        ("ctrl+c", "request_quit", "取消/退出"),
    ]

    def __init__(
        self,
        turn_runner: TurnRunner,
        provider_config: ProviderConfig,
        session_controller: SessionController,
        ui_config: UIConfig,
        slash_command_registry: SlashCommandRegistry | None = None,
    ) -> None:
        super().__init__()
        self._turn_runner = turn_runner
        self._provider_config = provider_config
        self._session_controller = session_controller
        self._ui_config = ui_config
        self._slash_command_registry = slash_command_registry or create_default_slash_command_registry()
        self._is_streaming = False
        self._chat_started = False
        self._message_widgets: dict[str, MessageWidget] = {}
        self._status_hint = "Ready"
        self._slash_menu_matches: list[SlashCommandDefinition] = []
        self._slash_menu_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield BannerWidget(Path.cwd())
            yield VerticalScroll(id="chat-view")
            with Vertical(id="composer-region"):
                yield SlashCommandMenu(self._slash_command_registry.list_all())
                with Horizontal(id="composer"):
                    yield Static("✦", id="prompt-glyph")
                    yield ComposerTextArea(
                        "",
                        soft_wrap=True,
                        show_line_numbers=False,
                        compact=True,
                        highlight_cursor_line=False,
                        placeholder="",
                        id="composer-input",
                    )
                yield CommandHintBar()
            with Horizontal(id="status-bar"):
                yield Static(id="status-left")
                yield Static(id="status-center")
                yield Static(id="status-right")

    def on_mount(self) -> None:
        self.query_one(ComposerTextArea).focus()
        self._update_composer_height()
        self._refresh_composer_placeholder()
        self._refresh_command_ui()
        self._refresh_status_bar()

    def on_resize(self) -> None:
        self.call_after_refresh(self._update_composer_height)

    async def action_request_quit(self) -> None:
        if self._is_streaming:
            if self._turn_runner.cancel_active_turn():
                self._status_hint = "Cancelling..."
                self._refresh_status_bar()
            return
        self.exit(0)

    @on(TextArea.Changed, "#composer-input")
    def handle_composer_changed(self) -> None:
        self._update_composer_height()
        self._refresh_command_ui()

    @on(SlashMenuNavigateRequested)
    def handle_slash_menu_navigation(self, event: SlashMenuNavigateRequested) -> None:
        self._move_slash_menu(event.direction)

    @on(SlashMenuAcceptRequested)
    def handle_slash_menu_accept(self) -> None:
        self._accept_slash_menu_selection()

    @on(SlashMenuDismissRequested)
    def handle_slash_menu_dismiss(self) -> None:
        self._dismiss_slash_menu()

    @on(SlashCommandChosen)
    def handle_slash_command_chosen(self, event: SlashCommandChosen) -> None:
        self._accept_slash_command(event.command_name)

    @on(ComposerSubmitted)
    async def handle_input_submitted(self, event: ComposerSubmitted) -> None:
        text = event.value.strip()
        event.composer.clear()
        self._refresh_command_ui()
        if not text:
            return
        if self._is_streaming:
            return

        slash_match = self._slash_command_registry.parse_submission(
            text,
            self._session_controller.runtime_mode,
        )
        if slash_match is not None:
            payload = self._execute_slash_command(slash_match.definition.name, slash_match.arguments_text)
            if payload is None:
                return
            text = payload

        if not self._chat_started:
            self._chat_started = True
            self.query_one(BannerWidget).set_compact(True)
            self.query_one("#chat-view", VerticalScroll).set_class(True, "-banner-collapsed")

        self._is_streaming = True
        self._status_hint = "Busy"
        event.composer.disabled = True
        self._refresh_status_bar()
        self.process_prompt(text)

    @work(exclusive=False, exit_on_error=False)
    async def process_prompt(self, text: str) -> None:
        try:
            async for event in self._turn_runner.run_user_turn(text):
                await self._consume_turn_event(event)
        finally:
            self._is_streaming = False
            self._status_hint = "Ready"
            input_widget = self.query_one("#composer-input", ComposerTextArea)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_composer_placeholder()
            self._refresh_command_ui()
            self._refresh_status_bar()
            self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)

    def _refresh_status_bar(self) -> None:
        usage = self._session_controller.total_usage()
        api_type = "OpenAI" if self._provider_config.protocol == "openai" else "Claude"
        mode_label = "PLAN" if self._session_controller.runtime_mode == "plan" else "NORMAL"
        self.query_one("#status-left", Static).update(f"{self._provider_config.model} ({api_type})")
        center_text = self._status_hint or ("Busy" if self._is_streaming else "Ready")
        self.query_one("#status-center", Static).update(f"{center_text} [{mode_label}]")
        self.query_one("#status-right", Static).update(f"Tokens In {usage.input_tokens} | Out {usage.output_tokens}")

    async def _mount_message_widget(self, message: SessionMessage) -> None:
        chat_view = self.query_one("#chat-view", VerticalScroll)
        widget = MessageWidget(message, show_thinking=self._ui_config.show_thinking_status)
        self._message_widgets[message.id] = widget
        await chat_view.mount(widget)

    def _sync_message_widget(self, message_id: str) -> None:
        widget = self._message_widgets[message_id]
        widget.update_from_message(self._session_controller.get_message(message_id))

    async def _consume_turn_event(self, event: TurnEvent) -> None:
        self._apply_turn_event(event)
        if event.message is not None and event.kind in {"user_message_created", "assistant_message_started"}:
            await self._mount_message_widget(event.message)
        elif event.message is not None:
            self._sync_message_widget(event.message.id)
        self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)
        self._refresh_status_bar()

    def _apply_turn_event(self, event: TurnEvent) -> None:
        if event.kind == "mode_changed":
            self._status_hint = event.progress_message or "Mode changed"
            self._refresh_composer_placeholder()
            self._refresh_command_ui()
            return
        if event.kind == "progress_updated":
            self._status_hint = event.progress_message or ("Busy" if self._is_streaming else "Ready")
            return
        if event.kind == "turn_cancelled":
            self._status_hint = event.progress_message or "Cancelled"
            return
        if event.kind == "turn_failed":
            self._status_hint = event.error_text or "Failed"
            return
        if event.kind == "assistant_message_completed":
            self._status_hint = "Ready"
            return
        if event.kind in {"assistant_text_delta", "tool_call_started", "tool_result_received", "usage_updated"}:
            self._status_hint = "Busy"

    def _refresh_composer_placeholder(self) -> None:
        composer = self.query_one("#composer-input", ComposerTextArea)
        if self._session_controller.runtime_mode == "plan":
            composer.placeholder = PLAN_PLACEHOLDER
            return
        composer.placeholder = NORMAL_PLACEHOLDER

    def _refresh_command_ui(self) -> None:
        composer = self.query_one("#composer-input", ComposerTextArea)
        menu = self.query_one(SlashCommandMenu)
        hint_bar = self.query_one(CommandHintBar)
        composer.clear_accepted_slash_command_if_needed()

        menu_query = extract_slash_menu_query(composer.text)
        active_name = self._current_active_slash_name()
        if menu_query is not None and not composer.should_suppress_slash_menu():
            matches = self._slash_command_registry.suggest(menu_query, self._session_controller.runtime_mode)
            match_names = [command.name for command in matches]
            if active_name in match_names:
                self._slash_menu_index = match_names.index(active_name)
            else:
                self._slash_menu_index = 0
            self._slash_menu_matches = matches
            active_name = self._current_active_slash_name()
            menu.set_matches(matches, active_name)
            composer.slash_menu_active = bool(matches)
            if matches and active_name is not None:
                active_command = self._slash_command_registry.get(active_name)
                hint_bar.set_hint(active_command.hint_text if active_command is not None else DEFAULT_COMMAND_HINT)
                return
            hint_bar.set_hint(DEFAULT_COMMAND_HINT)
            return

        self._slash_menu_matches = []
        self._slash_menu_index = 0
        composer.slash_menu_active = False
        menu.set_matches([], None)

        command_name = extract_exact_command_name(composer.text)
        if command_name is not None:
            command = self._slash_command_registry.get(command_name)
            if command is not None:
                hint_bar.set_hint(command.hint_text)
                return

        hint_bar.set_hint(DEFAULT_COMMAND_HINT)

    def _move_slash_menu(self, direction: int) -> None:
        if not self._slash_menu_matches:
            return
        self._slash_menu_index = (self._slash_menu_index + direction) % len(self._slash_menu_matches)
        self._refresh_command_ui()

    def _accept_slash_menu_selection(self) -> None:
        command_name = self._current_active_slash_name()
        if command_name is None:
            return
        self._accept_slash_command(command_name)

    def _accept_slash_command(self, command_name: str) -> None:
        command = self._slash_command_registry.get(command_name)
        if command is None:
            return

        composer = self.query_one("#composer-input", ComposerTextArea)
        composer.text = command.insert_text
        composer.cursor_location = composer.document.end
        composer.remember_accepted_slash_command(command.insert_text)
        composer.focus()
        self._refresh_command_ui()

    def _dismiss_slash_menu(self) -> None:
        self._slash_menu_matches = []
        self._slash_menu_index = 0
        composer = self.query_one("#composer-input", ComposerTextArea)
        composer.slash_menu_active = False
        self.query_one(SlashCommandMenu).set_matches([], None)

        command_name = extract_exact_command_name(composer.text)
        if command_name is not None:
            command = self._slash_command_registry.get(command_name)
            if command is not None:
                self.query_one(CommandHintBar).set_hint(command.hint_text)
                return
        self.query_one(CommandHintBar).set_hint(DEFAULT_COMMAND_HINT)

    def _current_active_slash_name(self) -> str | None:
        if not self._slash_menu_matches:
            return None
        if self._slash_menu_index >= len(self._slash_menu_matches):
            self._slash_menu_index = 0
        return self._slash_menu_matches[self._slash_menu_index].name

    def _execute_slash_command(self, command_name: str, arguments_text: str) -> str | None:
        if command_name == "exit":
            self.exit(0)
            return None

        if command_name == "do":
            self._apply_turn_event(self._turn_runner.set_mode("normal"))
            self._refresh_status_bar()
            return None

        if command_name == "plan":
            self._apply_turn_event(self._turn_runner.set_mode("plan"))
            self._refresh_status_bar()
            payload = arguments_text.strip()
            if not payload:
                return None
            return payload

        return None

    def _update_composer_height(self) -> None:
        composer_input = self.query_one("#composer-input", ComposerTextArea)
        composer = self.query_one("#composer", Horizontal)

        visible_lines = max(
            MIN_COMPOSER_LINES,
            min(MAX_COMPOSER_LINES, composer_input.wrapped_document.height),
        )
        composer_input.styles.height = str(visible_lines)
        composer.styles.height = str(visible_lines + COMPOSER_FRAME_HEIGHT)


class ChatTUI:
    def __init__(
        self,
        turn_runner: TurnRunner,
        provider_config: ProviderConfig,
        session_controller: SessionController,
        ui_config: UIConfig,
    ) -> None:
        self._app = LanCherTextualApp(
            turn_runner=turn_runner,
            provider_config=provider_config,
            session_controller=session_controller,
            ui_config=ui_config,
        )

    async def run(self) -> int:
        result = await self._app.run_async()
        return 0 if result is None else result
