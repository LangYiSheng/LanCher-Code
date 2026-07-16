from __future__ import annotations

import asyncio
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, TextArea

from lancher_code.models import (
    MessageUsage,
    PermissionRequest,
    PermissionResolution,
    ProviderConfig,
    RuntimeMode,
    SessionMessage,
    TurnEvent,
    UIConfig,
)
from lancher_code.mcp.manager import MCPClientManager, MCPInitializationProgress
from lancher_code.settings_service import SettingsService
from lancher_code.logging_system import get_logger
from lancher_code.session import SessionController
from lancher_code.slash_commands import (
    SlashCommandDefinition,
    SlashCommandRegistry,
    create_default_slash_command_registry,
    extract_exact_command_name,
    extract_slash_menu_query,
)
from lancher_code.tui_views.composer import (
    CommandHintBar,
    ComposerSubmitted,
    ComposerTextArea,
    PermissionModeCycleRequested,
    SlashCommandChosen,
    SlashCommandMenu,
    SlashMenuAcceptRequested,
    SlashMenuDismissRequested,
    SlashMenuNavigateRequested,
)
from lancher_code.tui_views.message import BannerWidget, MessageWidget
from lancher_code.tui_views.permission import InlinePermissionPanel
from lancher_code.tui_views.settings import SettingsResult, SettingsScreen
from lancher_code.turn_runner import TurnRunner
from lancher_code.tools.core.registry import ToolRegistry

logger = get_logger("tui.chat")

MIN_COMPOSER_LINES = 1
MAX_COMPOSER_LINES = 6
COMPOSER_FRAME_HEIGHT = 1
DEFAULT_COMMAND_HINT = ""
DEFAULT_PLACEHOLDER = "发送一条消息"
PLAN_PLACEHOLDER = "Plan Mode: 继续补充或修改计划"
MCP_PLACEHOLDER = "正在初始化 MCP，请稍候…"
MODE_SEQUENCE: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass")
MODE_GLYPHS: dict[RuntimeMode, str] = {
    "default": ">",
    "plan": "#",
    "acceptEdits": "+",
    "bypass": "!",
}
MODE_STATUS_LABELS: dict[RuntimeMode, str] = {
    "default": "",
    "plan": "计划模式",
    "acceptEdits": "允许编辑",
    "bypass": "完全访问",
}


class LanCherTextualApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
        color: #f2f2f2;
    }

    #root {
        height: 100%;
        width: 100%;
        layout: vertical;
    }

    #banner {
        margin: 1 2 0 2;
        padding: 0;
        border: none;
        width: 1fr;
    }

    #banner.-compact {
        margin: 0 2;
        padding: 0;
        height: auto;
        color: #7f9ab8;
    }

    #chat-view {
        margin: 1 1 0 1;
        padding: 1;
        border: none;
        height: 1fr;
        width: 100%;
    }

    .message {
        margin: 0 0 2 0;
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
        margin: 1 2 0 2;
        width: 1fr;
        layout: vertical;
        height: auto;
    }

    #slash-command-menu {
        border-left: solid #4b6f97;
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
        border-top: solid #4b6f97;
        height: 2;
        min-height: 2;
        max-height: 7;
        layout: horizontal;
        width: 1fr;
        padding: 0 1;
    }

    #composer:focus-within {
        border-top: solid #73b6ff;
    }

    #prompt-glyph {
        width: 2;
        content-align: center middle;
        color: #73b6ff;
        text-style: bold;
    }

    #prompt-glyph.-default {
        color: #73b6ff;
    }

    #prompt-glyph.-plan {
        color: #f5c451;
    }

    #prompt-glyph.-acceptEdits {
        color: #78d98a;
    }

    #prompt-glyph.-bypass {
        color: #ff9b6b;
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

    #inline-permission-panel {
        border-left: solid #4b6f97;
        padding: 1 2;
        width: 1fr;
        height: auto;
        background: #0f1a26;
    }

    #inline-permission-panel:focus {
        border-left: solid #73b6ff;
    }

    #permission-title {
        color: #73b6ff;
        text-style: bold;
        margin-bottom: 1;
        height: auto;
    }

    #permission-command, #permission-details {
        color: #f2f2f2;
        margin-left: 2;
        height: auto;
    }

    #permission-description {
        color: #a8b9cc;
        margin: 0 0 1 2;
        height: auto;
    }

    #permission-prompt {
        color: #c8d5e3;
        margin-bottom: 1;
        height: auto;
    }

    .permission-preview {
        color: #c8d5e3;
        height: auto;
        margin-left: 2;
    }

    .permission-preview.-error {
        color: #ff7b72;
    }

    .permission-preview.-success {
        color: #78d98a;
    }

    .permission-option {
        width: 1fr;
        height: auto;
        padding: 0 1;
        color: #c8d5e3;
    }

    .permission-option.-active {
        background: #203246;
    }

    #permission-help {
        color: #7f9ab8;
        margin-top: 1;
        height: auto;
    }

    #status-bar {
        margin: 1 1 1 1;
        height: 1;
        layout: horizontal;
        color: #97adc7;
        width: 1fr;
    }

    #status-left {
        width: 1fr;
    }

    #status-left.-plan {
        color: #f5c451;
    }

    #status-left.-acceptEdits {
        color: #78d98a;
    }

    #status-left.-bypass {
        color: #ff9b6b;
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
        mcp_manager: MCPClientManager | None = None,
        tool_registry: ToolRegistry | None = None,
        settings_service: SettingsService | None = None,
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
        self._permission_resolution_future: asyncio.Future[PermissionResolution] | None = None
        self._mcp_manager = mcp_manager
        self._tool_registry = tool_registry
        self._settings_service = settings_service
        self.mcp_initialization_complete = mcp_manager is None or not mcp_manager.has_servers

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield BannerWidget(Path.cwd())
            yield VerticalScroll(id="chat-view")
            with Vertical(id="composer-region"):
                yield SlashCommandMenu(self._slash_command_registry.list_all())
                with Horizontal(id="composer"):
                    yield Static(MODE_GLYPHS["default"], id="prompt-glyph")
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
        composer = self.query_one(ComposerTextArea)
        composer.disabled = not self.mcp_initialization_complete
        if self.mcp_initialization_complete:
            composer.focus()
        self._update_composer_height()
        self._refresh_mode_chrome()
        self._refresh_composer_placeholder()
        self._refresh_command_ui()
        self._refresh_status_bar()
        if self._mcp_manager is not None:
            self._mcp_manager.add_progress_callback(self._handle_mcp_progress)
            if self._mcp_manager.has_servers:
                self._status_hint = "Initializing MCP"
                self.initialize_mcp()
            else:
                self._handle_mcp_progress(MCPInitializationProgress(0, 0, 0, 0, 0, None, "complete"))

    @work(exclusive=True, exit_on_error=False)
    async def initialize_mcp(self) -> None:
        try:
            if self._mcp_manager is not None and self._tool_registry is not None:
                await self._mcp_manager.initialize(self._tool_registry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "event=tui_mcp_worker_failed exception_type=%s", type(exc).__name__
            )
        finally:
            self.mcp_initialization_complete = True
            composer = self.query_one("#composer-input", ComposerTextArea)
            composer.disabled = False
            self._status_hint = "Ready"
            self._refresh_composer_placeholder()
            self._refresh_status_bar()
            composer.focus()

    def _handle_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        self.query_one(BannerWidget).update_mcp_progress(progress)

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

    @on(PermissionModeCycleRequested)
    def handle_permission_mode_cycle_requested(self) -> None:
        if self._is_streaming:
            return
        next_mode = _next_runtime_mode(self._session_controller.runtime_mode)
        self._apply_turn_event(self._turn_runner.set_mode(next_mode))
        self._refresh_status_bar()
        self.query_one("#composer-input", ComposerTextArea).focus()

    @on(ComposerSubmitted)
    async def handle_input_submitted(self, event: ComposerSubmitted) -> None:
        if not self.mcp_initialization_complete:
            return
        text = event.value.strip()
        event.composer.clear()
        self._refresh_command_ui()
        if not text or self._is_streaming:
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

    @on(InlinePermissionPanel.Resolved)
    def handle_permission_resolved(self, event: InlinePermissionPanel.Resolved) -> None:
        event.stop()
        future = self._permission_resolution_future
        if future is not None and not future.done():
            future.set_result(event.resolution)

    @work(exclusive=False, exit_on_error=False)
    async def process_prompt(self, text: str) -> None:
        try:
            async for event in self._turn_runner.run_user_turn(text):
                await self._consume_turn_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "event=tui_turn_worker_failed exception_type=%s", type(exc).__name__
            )
        finally:
            self._is_streaming = False
            self._status_hint = "Ready"
            input_widget = self.query_one("#composer-input", ComposerTextArea)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_mode_chrome()
            self._refresh_composer_placeholder()
            self._refresh_command_ui()
            self._refresh_status_bar()
            self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)

    def _refresh_status_bar(self) -> None:
        usage = self._session_controller.total_usage()
        center_text = self._status_hint or ("Busy" if self._is_streaming else "Ready")
        status_left = self.query_one("#status-left", Static)
        status_center = self.query_one("#status-center", Static)
        status_right = self.query_one("#status-right", Static)

        status_left.update(self._status_left_text())
        for candidate in MODE_SEQUENCE:
            status_left.set_class(candidate != "default" and candidate == self._session_controller.runtime_mode, f"-{candidate}")

        status_center.update(center_text)
        status_right.update(self._format_usage_text(usage))

    def _status_left_text(self) -> str:
        mode = self._session_controller.runtime_mode
        if mode == "default":
            api_type = "OpenAI" if self._provider_config.protocol == "openai" else "Claude"
            return f"{self._provider_config.model} ({api_type})"
        return MODE_STATUS_LABELS[mode]

    @staticmethod
    def _format_usage_text(usage: MessageUsage) -> str:
        input_text = f"Tokens In {usage.input_tokens}"
        if usage.cached_input_tokens > 0:
            input_text += f" (cached {usage.cached_input_tokens})"
        return f"{input_text} | Out {usage.output_tokens}"

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
        if event.kind == "permission_request_created" and event.permission_request is not None:
            resolution = await self._request_inline_permission(event.permission_request)
            if resolution is not None:
                self._turn_runner.resolve_permission_request(resolution)
        self.query_one("#chat-view", VerticalScroll).scroll_end(animate=False)
        self._refresh_status_bar()

    async def _request_inline_permission(self, request: PermissionRequest) -> PermissionResolution:
        composer = self.query_one("#composer", Horizontal)
        slash_menu = self.query_one(SlashCommandMenu)
        hint_bar = self.query_one(CommandHintBar)
        panel = InlinePermissionPanel(request)
        composer.display = False
        slash_menu.display = False
        hint_bar.display = False
        self._permission_resolution_future = asyncio.get_running_loop().create_future()
        await self.query_one("#composer-region", Vertical).mount(panel)
        panel.focus()
        try:
            return await self._permission_resolution_future
        finally:
            self._permission_resolution_future = None
            await panel.remove()
            composer.display = True
            self._refresh_command_ui()

    def _apply_turn_event(self, event: TurnEvent) -> None:
        if event.kind == "mode_changed":
            self._status_hint = event.progress_message or "Mode changed"
            self._refresh_mode_chrome()
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
        if event.kind == "permission_request_created":
            self._status_hint = "Waiting for permission"
            return
        if event.kind == "permission_request_resolved":
            self._status_hint = "Busy"
            return
        if event.kind in {"assistant_text_delta", "tool_call_started", "tool_result_received", "usage_updated"}:
            self._status_hint = "Busy"

    def _refresh_mode_chrome(self) -> None:
        mode = self._session_controller.runtime_mode
        glyph = self.query_one("#prompt-glyph", Static)
        glyph.update(MODE_GLYPHS[mode])
        for candidate in MODE_SEQUENCE:
            glyph.set_class(candidate == mode, f"-{candidate}")

    def _refresh_composer_placeholder(self) -> None:
        composer = self.query_one("#composer-input", ComposerTextArea)
        if not self.mcp_initialization_complete:
            composer.placeholder = MCP_PLACEHOLDER
            return
        if self._session_controller.runtime_mode == "plan":
            composer.placeholder = PLAN_PLACEHOLDER
            return
        composer.placeholder = DEFAULT_PLACEHOLDER

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
            self._apply_turn_event(self._turn_runner.restore_mode_after_plan())
            self._refresh_status_bar()
            return None

        if command_name == "plan":
            self._apply_turn_event(self._turn_runner.set_mode("plan"))
            self._refresh_status_bar()
            payload = arguments_text.strip()
            if not payload:
                return None
            return payload

        if command_name == "mode":
            requested_mode = arguments_text.strip()
            if requested_mode not in set(MODE_SEQUENCE):
                self._status_hint = "Unknown mode"
                self._refresh_status_bar()
                return None
            self._apply_turn_event(self._turn_runner.set_mode(requested_mode))  # type: ignore[arg-type]
            self._refresh_status_bar()
            return None

        if command_name == "settings":
            if self._settings_service is None:
                self._status_hint = "Settings unavailable"
                self._refresh_status_bar()
                return None
            self.push_screen(SettingsScreen(self._settings_service), self._handle_settings_result)
            return None

        return None

    def _handle_settings_result(self, result: SettingsResult | None) -> None:
        if result is not None and result.saved:
            self._status_hint = "Settings saved · restart for model/MCP"
        else:
            self._status_hint = "Ready"
        self._refresh_status_bar()
        self.query_one("#composer-input", ComposerTextArea).focus()

    def _update_composer_height(self) -> None:
        composer_input = self.query_one("#composer-input", ComposerTextArea)
        composer = self.query_one("#composer", Horizontal)

        visible_lines = max(
            MIN_COMPOSER_LINES,
            min(MAX_COMPOSER_LINES, composer_input.wrapped_document.height),
        )
        composer_input.styles.height = str(visible_lines)
        composer.styles.height = str(visible_lines + COMPOSER_FRAME_HEIGHT)


def _next_runtime_mode(current_mode: RuntimeMode) -> RuntimeMode:
    current_index = MODE_SEQUENCE.index(current_mode)
    return MODE_SEQUENCE[(current_index + 1) % len(MODE_SEQUENCE)]


class ChatTUI:
    def __init__(
        self,
        turn_runner: TurnRunner,
        provider_config: ProviderConfig,
        session_controller: SessionController,
        ui_config: UIConfig,
        mcp_manager: MCPClientManager | None = None,
        tool_registry: ToolRegistry | None = None,
        settings_service: SettingsService | None = None,
    ) -> None:
        self._app = LanCherTextualApp(
            turn_runner=turn_runner,
            provider_config=provider_config,
            session_controller=session_controller,
            ui_config=ui_config,
            mcp_manager=mcp_manager,
            tool_registry=tool_registry,
            settings_service=settings_service,
        )

    async def run(self) -> int:
        result = await self._app.run_async()
        return 0 if result is None else result

    def configure_mcp(self, manager: MCPClientManager, registry: ToolRegistry) -> None:
        self._app._mcp_manager = manager
        self._app._tool_registry = registry
        self._app.mcp_initialization_complete = not manager.has_servers

    def configure_settings(self, service: SettingsService) -> None:
        self._app._settings_service = service
