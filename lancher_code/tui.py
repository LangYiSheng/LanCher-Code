from __future__ import annotations

from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Message
from textual.widgets import Static, TextArea

from lancher_code.models import ProviderConfig, SessionMessage, TraceEntry, UIConfig
from lancher_code.session import SessionController
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


class ComposerSubmitted(Message):
    def __init__(self, composer: "ComposerTextArea", value: str) -> None:
        super().__init__()
        self.composer = composer
        self.value = value


class ComposerTextArea(TextArea):
    BINDINGS = [
        Binding("enter", "submit_message", "发送", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "换行", show=False, priority=True),
    ] + TextArea.BINDINGS

    def action_submit_message(self) -> None:
        self.post_message(ComposerSubmitted(self, self.text))

    def action_insert_newline(self) -> None:
        self.insert("\n")


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
        header.update(f"{marker} 思考内容 ({len(self._entries)})")
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
        self.set_class(self.status == "error", "-error")

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
        if self.status == "error":
            return "ERROR"
        return self.ROLE_LABELS.get(self.role, self.role.upper())

    def _label_color(self) -> str:
        if self.status == "error":
            return "#ff7b72"
        return self.ROLE_STYLES.get(self.role, "#ffffff")

    def _body_text(self) -> str:
        if self.status == "error":
            return self.content or "请求失败。"
        if self.content:
            return self.content
        if self.status == "streaming" and not self.trace_entries:
            return "等待模型回复..."
        if self.status == "complete" and not self.trace_entries:
            return "本轮未收到任何回复。"
        return ""

    def _body_color(self) -> str:
        if self.status == "error":
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

    #composer {
        margin: 1 1 0 1;
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
        ("ctrl+c", "request_quit", "退出"),
    ]

    def __init__(
        self,
        turn_runner: TurnRunner,
        provider_config: ProviderConfig,
        session_controller: SessionController,
        ui_config: UIConfig,
    ) -> None:
        super().__init__()
        self._turn_runner = turn_runner
        self._provider_config = provider_config
        self._session_controller = session_controller
        self._ui_config = ui_config
        self._is_streaming = False
        self._chat_started = False
        self._message_widgets: dict[str, MessageWidget] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield BannerWidget(Path.cwd())
            yield VerticalScroll(id="chat-view")
            with Horizontal(id="composer"):
                yield Static("❯", id="prompt-glyph")
                yield ComposerTextArea(
                    "",
                    soft_wrap=True,
                    show_line_numbers=False,
                    compact=True,
                    highlight_cursor_line=False,
                    placeholder="发送一条消息... Enter发送，Shift+Enter换行，/exit 或 Ctrl+C 退出",
                    id="composer-input",
                )
            with Horizontal(id="status-bar"):
                yield Static(id="status-left")
                yield Static(id="status-center")
                yield Static(id="status-right")

    def on_mount(self) -> None:
        self.query_one(ComposerTextArea).focus()
        self._update_composer_height()
        self._refresh_status_bar()

    def on_resize(self) -> None:
        self.call_after_refresh(self._update_composer_height)

    async def action_request_quit(self) -> None:
        self.exit(0)

    @on(TextArea.Changed, "#composer-input")
    def handle_composer_changed(self) -> None:
        self._update_composer_height()

    @on(ComposerSubmitted)
    async def handle_input_submitted(self, event: ComposerSubmitted) -> None:
        text = event.value.strip()
        event.composer.clear()
        if not text:
            return
        if text == "/exit":
            self.exit(0)
            return
        if self._is_streaming:
            return

        if not self._chat_started:
            self._chat_started = True
            self.query_one(BannerWidget).set_compact(True)
            self.query_one("#chat-view", VerticalScroll).set_class(True, "-banner-collapsed")

        self._is_streaming = True
        event.composer.disabled = True
        self._refresh_status_bar()
        self.process_prompt(text)

    @work(exclusive=False, exit_on_error=False)
    async def process_prompt(self, text: str) -> None:
        try:
            async for event in self._turn_runner.run_user_turn(text):
                if event.message is not None:
                    if event.kind in {"user_message_created", "assistant_message_started"}:
                        await self._mount_message_widget(event.message)
                    else:
                        self._sync_message_widget(event.message.id)

                self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)
                self._refresh_status_bar()
        finally:
            self._is_streaming = False
            input_widget = self.query_one("#composer-input", ComposerTextArea)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_status_bar()
            self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)

    def _refresh_status_bar(self) -> None:
        usage = self._session_controller.total_usage()
        api_type = "OpenAI" if self._provider_config.protocol == "openai" else "Claude"
        self.query_one("#status-left", Static).update(f"{self._provider_config.model} ({api_type})")
        self.query_one("#status-center", Static).update("Busy" if self._is_streaming else "Ready")
        self.query_one("#status-right", Static).update(f"Tokens In {usage.input_tokens} | Out {usage.output_tokens}")

    async def _mount_message_widget(self, message: SessionMessage) -> None:
        chat_view = self.query_one("#chat-view", VerticalScroll)
        widget = MessageWidget(message, show_thinking=self._ui_config.show_thinking_status)
        self._message_widgets[message.id] = widget
        await chat_view.mount(widget)

    def _sync_message_widget(self, message_id: str) -> None:
        widget = self._message_widgets[message_id]
        widget.update_from_message(self._session_controller.get_message(message_id))

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
