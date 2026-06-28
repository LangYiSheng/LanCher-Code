from __future__ import annotations

from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Message
from textual.widgets import Static, TextArea

from lancher_code.errors import LanCherError
from lancher_code.models import ChatRequest, MessageUsage, ProviderConfig, SessionMessage, UIConfig
from lancher_code.providers.base import ChatProvider
from lancher_code.session import SessionController

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


class MessageLabelWidget(Static):
    def update_label(self, text: str, color: str) -> None:
        self.styles.color = color
        self.styles.text_style = "bold"
        self.update(text)
        self.refresh(layout=True)


class MessageThinkingWidget(Static):
    def show_thinking(self, text: str) -> None:
        self.display = True
        self.update(text)
        self.refresh(layout=True)

    def hide_thinking(self) -> None:
        self.display = False
        self.update("")
        self.refresh(layout=True)


class MessageBodyWidget(Static):
    def show_body(self, text: str, color: str) -> None:
        self.styles.color = color
        self.display = True
        self.update(text)
        self.refresh(layout=True)

    def hide_body(self) -> None:
        self.display = False
        self.update("")
        self.refresh(layout=True)


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
        self.thinking = message.thinking
        self.status = message.status

    @property
    def thinking_collapsed(self) -> bool:
        return bool(self.content)

    def compose(self) -> ComposeResult:
        yield MessageLabelWidget(classes="message-label")
        yield MessageThinkingWidget(classes="message-thinking")
        yield MessageBodyWidget(classes="message-body")

    def on_mount(self) -> None:
        self._sync_view()

    def update_from_message(self, message: SessionMessage) -> None:
        self.role = message.role
        self.content = message.content
        self.thinking = message.thinking
        self.status = message.status
        self._sync_view()

    def _sync_view(self) -> None:
        self.set_class(self.status == "error", "-error")

        label_widget = self.query_one(MessageLabelWidget)
        label_widget.update_label(self._label_text(), self._label_color())

        thinking_widget = self.query_one(MessageThinkingWidget)
        if self._thinking_visible():
            thinking_widget.show_thinking(self.thinking)
        else:
            thinking_widget.hide_thinking()

        body_widget = self.query_one(MessageBodyWidget)
        body_text = self._body_text()
        if body_text:
            body_widget.show_body(body_text, self._body_color())
        else:
            body_widget.hide_body()

        self.refresh(layout=True)

    def _label_text(self) -> str:
        if self.status == "error":
            return "ERROR"
        return self.ROLE_LABELS.get(self.role, self.role.upper())

    def _label_color(self) -> str:
        if self.status == "error":
            return "#ff7b72"
        return self.ROLE_STYLES.get(self.role, "#ffffff")

    def _thinking_visible(self) -> bool:
        return self._show_thinking and bool(self.thinking) and not self.thinking_collapsed and self.status != "error"

    def _body_text(self) -> str:
        if self.status == "error":
            return self.content or "请求失败。"
        if self.content:
            return self.content
        if self.status == "streaming" and not self.thinking:
            return "等待模型回复..."
        if self.status == "complete" and not self.thinking:
            return "本轮未收到任何回复。"
        return ""

    def _body_color(self) -> str:
        if self.status == "error":
            return "#ff7b72"
        if self.content:
            return "#e8e8e8"
        return "#97adc7"


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

    .message-label {
        width: 1fr;
        height: auto;
    }

    .message-thinking {
        width: 1fr;
        height: auto;
        color: #a8b9cc;
        margin: 0 0 1 0;
    }

    .message-body {
        width: 1fr;
        height: auto;
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
        ("ctrl+d", "request_quit", "退出"),
    ]

    def __init__(
        self,
        provider: ChatProvider,
        provider_config: ProviderConfig,
        session_controller: SessionController,
        ui_config: UIConfig,
    ) -> None:
        super().__init__()
        self._provider = provider
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
                    placeholder="发送一条消息... Enter发送，Shift+Enter换行，/exit 或 Ctrl+C/Ctrl+D 退出",
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
        if text in {"/exit", "/quit"}:
            self.exit(0)
            return
        if self._is_streaming:
            return

        if not self._chat_started:
            self._chat_started = True
            self.query_one(BannerWidget).set_compact(True)
            self.query_one("#chat-view", VerticalScroll).set_class(True, "-banner-collapsed")

        user_message = self._session_controller.create_user_message(text)
        assistant_message = self._session_controller.create_assistant_message()
        request = self._session_controller.build_request()

        chat_view = self.query_one("#chat-view", VerticalScroll)
        await chat_view.mount(self._register_message_widget(user_message))
        await chat_view.mount(self._register_message_widget(assistant_message))
        chat_view.scroll_end(animate=False)

        self._is_streaming = True
        event.composer.disabled = True
        self._refresh_status_bar()
        self.process_prompt(request, assistant_message.id)

    @work(exclusive=False, exit_on_error=False)
    async def process_prompt(
        self,
        request: ChatRequest,
        assistant_message_id: str,
    ) -> None:
        try:
            async for event in self._provider.stream_chat(request):
                if event.kind == "thinking_delta" and event.text:
                    self._session_controller.append_message_thinking(assistant_message_id, event.text)
                elif event.kind == "text_delta" and event.text:
                    self._session_controller.append_message_content(assistant_message_id, event.text)
                elif event.kind == "message_end":
                    self._session_controller.complete_message(assistant_message_id, event.usage)

                self._sync_message_widget(assistant_message_id)
                self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)
                self._refresh_status_bar()

            assistant_message = self._session_controller.get_message(assistant_message_id)
            if assistant_message.status == "streaming":
                self._session_controller.complete_message(assistant_message_id, MessageUsage())
                self._sync_message_widget(assistant_message_id)
        except LanCherError as exc:
            self._session_controller.fail_message(assistant_message_id, exc.user_message)
            self._sync_message_widget(assistant_message_id)
        except Exception as exc:
            self._session_controller.fail_message(assistant_message_id, f"发生未预期异常: {exc}")
            self._sync_message_widget(assistant_message_id)
        finally:
            self._is_streaming = False
            input_widget = self.query_one("#composer-input", ComposerTextArea)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_status_bar()
            self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)

    def _refresh_status_bar(self) -> None:
        usage = self._session_controller.total_usage()
        provider_name = "Anthropic Claude" if self._provider_config.protocol == "claude" else "OpenAI"
        self.query_one("#status-left", Static).update(f"Provider: {provider_name}")
        self.query_one("#status-center", Static).update("Busy" if self._is_streaming else "Ready")
        self.query_one("#status-right", Static).update(
            f"{self._provider_config.model} · Tokens In {usage.input_tokens} · Out {usage.output_tokens}"
        )

    def _register_message_widget(self, message: SessionMessage) -> MessageWidget:
        widget = MessageWidget(message, show_thinking=self._ui_config.show_thinking_status)
        self._message_widgets[message.id] = widget
        return widget

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
        provider: ChatProvider,
        provider_config: ProviderConfig,
        session_controller: SessionController,
        ui_config: UIConfig,
    ) -> None:
        self._app = LanCherTextualApp(
            provider=provider,
            provider_config=provider_config,
            session_controller=session_controller,
            ui_config=ui_config,
        )

    async def run(self) -> int:
        result = await self._app.run_async()
        return 0 if result is None else result
