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
from lancher_code.models import ChatRequest, ProviderConfig, UIConfig
from lancher_code.providers.base import ChatProvider
from lancher_code.rendering import StreamAccumulator, estimate_token_count
from lancher_code.session import SessionController

BANNER_TEXT = r"""
    __                ________                 ______          __   
   / /   ____ _____  / ____/ /_  ___  _____   / ____/___  ____/ /__ 
  / /   / __ `/ __ \/ /   / __ \/ _ \/ ___/  / /   / __ \/ __  / _ \
 / /___/ /_/ / / / / /___/ / / /  __/ /     / /___/ /_/ / /_/ /  __/
/_____/\__,_/_/ /_/\____/_/ /_/\___/_/      \____/\____/\__,_/\___/ 
"""


class ComposerSubmitted(Message):
    def __init__(self, composer: "ComposerTextArea", value: str) -> None:
        super().__init__()
        self.composer = composer
        self.value = value


class ComposerTextArea(TextArea):
    BINDINGS = [
        Binding("enter", "submit_message", "发送", show=False, priority=True),
        Binding("alt+enter", "insert_newline", "换行", show=False, priority=True),
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


class MessageWidget(Vertical):
    ROLE_LABELS = {
        "user": "YOU",
        "assistant": "LANCHER",
        "error": "ERROR",
    }
    ROLE_STYLES = {
        "user": "#78d98a",
        "assistant": "#73b6ff",
        "error": "#ff7b72",
    }

    def __init__(self, role: str, content: str = "", status: str = "") -> None:
        super().__init__(classes=f"message message--{role}")
        self.role = role
        self.content = content
        self._status = status
        self._thinking = ""
        self._thinking_collapsed = False

    @property
    def thinking(self) -> str:
        return self._thinking

    @property
    def thinking_collapsed(self) -> bool:
        return self._thinking_collapsed

    def compose(self) -> ComposeResult:
        yield Static(classes="message-label")
        yield Static(classes="message-thinking")
        yield Static(classes="message-body")

    def on_mount(self) -> None:
        self._sync_view()

    def set_content(self, content: str) -> None:
        self.content = content
        self._status = ""
        if self._thinking:
            self._thinking_collapsed = True
        self._sync_view()

    def set_status(self, status: str) -> None:
        self._status = status
        self._sync_view()

    def set_thinking(self, thinking: str, *, collapsed: bool = False) -> None:
        self._thinking = thinking
        self._thinking_collapsed = collapsed
        self._sync_view()

    def _sync_view(self) -> None:
        label_widget = self.query_one(".message-label", Static)
        label_widget.update(
            self.ROLE_LABELS.get(self.role, self.role.upper()),
        )
        label_widget.styles.color = self.ROLE_STYLES.get(self.role, "#ffffff")
        label_widget.styles.text_style = "bold"

        thinking_widget = self.query_one(".message-thinking", Static)
        thinking_visible = bool(self._thinking and not self._thinking_collapsed)
        thinking_widget.display = thinking_visible
        if thinking_visible:
            thinking_widget.update(self._thinking)

        body_widget = self.query_one(".message-body", Static)
        body_text = self.content or self._status
        body_widget.display = bool(body_text)
        if body_text:
            body_widget.styles.color = "#e8e8e8" if self.content else "#97adc7"
            body_widget.update(body_text)


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

    .message--error {
        border-left: wide #ff7b72;
    }

    #composer {
        margin: 1 1 0 1;
        border: tall #4b6f97;
        height: 3;
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
        self._input_tokens_total = 0
        self._output_tokens_total = 0
        self._is_streaming = False
        self._chat_started = False

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
                    placeholder="发送一条消息... Enter发送，Alt+Enter换行，/exit 或 Ctrl+C/Ctrl+D 退出",
                    id="composer-input",
                )
            with Horizontal(id="status-bar"):
                yield Static(id="status-left")
                yield Static(id="status-center")
                yield Static(id="status-right")

    def on_mount(self) -> None:
        self.query_one(ComposerTextArea).focus()
        self._refresh_status_bar()

    async def action_request_quit(self) -> None:
        self.exit(0)

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

        self._session_controller.record_user_message(text)
        request = self._session_controller.build_request()
        estimated_prompt_tokens = sum(
            estimate_token_count(message.content) for message in request.messages
        )

        user_widget = MessageWidget("user", text)
        assistant_widget = MessageWidget("assistant", status="等待模型回复...")
        chat_view = self.query_one("#chat-view", VerticalScroll)
        await chat_view.mount(user_widget)
        await chat_view.mount(assistant_widget)
        chat_view.scroll_end(animate=False)

        self._is_streaming = True
        event.composer.disabled = True
        self._refresh_status_bar()
        self.process_prompt(request, assistant_widget, estimated_prompt_tokens)

    @work(exclusive=False, exit_on_error=False)
    async def process_prompt(
        self,
        request: ChatRequest,
        assistant_widget: MessageWidget,
        estimated_prompt_tokens: int,
    ) -> None:
        accumulator = StreamAccumulator()
        usage_received = False

        try:
            async for event in self._provider.stream_chat(request):
                accumulator.consume(event)

                if event.kind == "thinking_delta" and self._ui_config.show_thinking_status:
                    if accumulator.thinking:
                        assistant_widget.set_thinking(accumulator.thinking)
                    else:
                        assistant_widget.set_status("模型正在思考...")
                elif event.kind == "text_delta":
                    assistant_widget.set_content(accumulator.text)
                elif event.kind == "message_end":
                    usage_received = self._apply_usage(event.metadata.get("usage"))

                self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)
                self._refresh_status_bar()

            assistant_text = accumulator.text.strip()
            if assistant_text:
                self._session_controller.record_assistant_message(assistant_text)
                if not usage_received:
                    self._input_tokens_total += estimated_prompt_tokens
                    self._output_tokens_total += estimate_token_count(assistant_text)
            elif accumulator.thinking_seen:
                assistant_widget.set_status("思考完成，但当前未返回可显示文本。")
                if not usage_received:
                    self._input_tokens_total += estimated_prompt_tokens
            else:
                assistant_widget.set_status("本轮未收到任何回复。")
        except LanCherError as exc:
            assistant_widget.remove()
            await self._append_error_message(exc.user_message)
        except Exception as exc:
            assistant_widget.remove()
            await self._append_error_message(f"发生未预期异常: {exc}")
        finally:
            self._is_streaming = False
            input_widget = self.query_one("#composer-input", ComposerTextArea)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_status_bar()
            self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)

    async def _append_error_message(self, message: str) -> None:
        chat_view = self.query_one("#chat-view", VerticalScroll)
        await chat_view.mount(MessageWidget("error", message))
        chat_view.scroll_end(animate=False)

    def _apply_usage(self, usage: object) -> bool:
        if not isinstance(usage, dict) or not usage:
            return False

        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens is None:
            prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("completion_tokens")
        if completion_tokens is None:
            completion_tokens = usage.get("output_tokens")

        applied = False
        if isinstance(prompt_tokens, int):
            self._input_tokens_total += prompt_tokens
            applied = True
        if isinstance(completion_tokens, int):
            self._output_tokens_total += completion_tokens
            applied = True
        return applied

    def _refresh_status_bar(self) -> None:
        provider_name = "Anthropic Claude" if self._provider_config.protocol == "claude" else "OpenAI"
        self.query_one("#status-left", Static).update(f"Provider: {provider_name}")
        self.query_one("#status-center", Static).update("Busy" if self._is_streaming else "Ready")
        self.query_one("#status-right", Static).update(
            f"{self._provider_config.model} · Tokens In {self._input_tokens_total} · Out {self._output_tokens_total}"
        )


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
