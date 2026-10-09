from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, TextArea

from lancher_code.errors import LanCherError
from lancher_code.model_catalog import iter_model_refs, model_display_name
from lancher_code.models import (
    MessageUsage,
    PermissionRequest,
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
from lancher_code.session_store import SessionStoreError
from lancher_code.slash_commands import (
    SlashCompletionCandidate,
    SlashCompletionContext,
    SlashCommandRegistry,
    create_default_slash_command_registry,
    extract_exact_command_name,
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
from lancher_code.tui_views.model_picker import ModelPickerScreen
from lancher_code.tui_views.theme import apply_theme, theme_palette
from lancher_code.tui_views.chat_controls import (
    ChatAction, StageBar, PendingQueue, PendingInputEditor, PlanPanel,
    PlanReviewScreen, PermissionPolicyScreen, ReadOnlyDetailsScreen,
)
from lancher_code.turn_runner import TurnRunner
from lancher_code.tools.core.registry import ToolRegistry

logger = get_logger("tui.chat")

MIN_COMPOSER_LINES = 1
MAX_COMPOSER_LINES = 6
COMPOSER_FRAME_HEIGHT = 1
DEFAULT_COMMAND_HINT = ""
DEFAULT_PLACEHOLDER = "发送一条消息"
PLAN_PLACEHOLDER = "补充或修改计划，确认后再开始执行"
MCP_PLACEHOLDER = "正在初始化 MCP，请稍候…"
MODE_SEQUENCE: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass")
MODE_GLYPHS: dict[RuntimeMode, str] = {
    "default": ">",
    "plan": "#",
    "acceptEdits": "+",
    "bypass": "!",
}

CONTEXT_REFRESH_EVENTS: frozenset[str] = frozenset(
    {
        "user_message_created",
        "usage_updated",
        "tool_result_received",
        "progress_updated",
        "assistant_message_completed",
        "turn_cancelled",
        "turn_failed",
    }
)


class LanCherTextualApp(App[int]):
    CSS = """
    #root { height: 100%; width: 100%; max-width: 112; layout: vertical; }
    #chat-view { height: 1fr; width: 100%; }
    .message { width: 100%; height: auto; layout: vertical; }
    .message-label, .message-body { height: auto; width: 1fr; }
    #chat-view.-banner-collapsed { margin-top: 0; }
    #composer-region { width: 1fr; height: auto; layout: vertical; }
    #composer { height: 2; min-height: 2; max-height: 7; width: 1fr; }
    #prompt-glyph { width: 2; content-align: center middle; text-style: bold; }
    #composer-input { width: 1fr; height: 100%; min-height: 1; max-height: 6; margin: 0; padding: 0; background: transparent; border: none; }
    #composer-input:focus { border: none; background: transparent; }
    #composer-input .text-area--cursor-line { background: transparent; }
    #slash-command-menu { display: none; width: 1fr; height: auto; }
    .slash-command-item { padding: 0 1; width: 1fr; height: auto; }
    #command-hint { width: 1fr; }
    #inline-permission-panel { width: 1fr; }
    #permission-title, #permission-command, #permission-details, #permission-description,
    #permission-prompt, .permission-preview, .permission-option, #permission-help { height: auto; }
    #permission-title { text-style: bold; }
    .permission-option { width: 1fr; padding: 0 1; }
    #status-bar { width: 1fr; }
    #status-center, #status-right { text-align: right; }
    Screen { background: $background; color: $text; align-horizontal: center; }
    #banner { margin: 1 2 0 2; height: auto; color: $text-muted; }
    #banner.-compact { margin: 0 2; }
    #stage-bar { margin: 0 2; height: 1; width: 1fr; }
    .stage-arrow { width: 2; height: 1; color: $text-muted; content-align: center middle; }
    #phase-explanation { width: 1fr; height: 1; color: $text-muted; padding-left: 2; }
    .quiet-action { border: none; height: 1; min-height: 1; min-width: 4; width: auto; padding: 0 1; background: transparent; color: $text-muted; text-style: none; }
    .quiet-action:hover { background: $surface; color: $text; }
    .quiet-action.-selected { color: $primary; text-style: bold; }
    .quiet-action:focus { background: $foreground; color: $background; text-style: bold; }
    #chat-view { margin: 1 1 0 1; padding: 0 1; }
    .message { padding: 0; margin: 0 0 1 0; border: none; }
    .message--user, .message--assistant, .message--system, .message.-error { border: none; }
    .message--user { border-left: solid $panel; padding-left: 1; }
    .message-label { color: $text-muted; }
    .trace-section { height: auto; width: 1fr; margin: 0; }
    .trace-header, .trace-body { height: auto; width: 1fr; }
    .trace-section:focus .trace-header { color: $primary; text-style: underline; }
    .trace-body { margin: 0 0 1 2; color: $text-muted; }
    #composer-region { margin: 0 2; max-height: 75%; }
    #composer { border-top: solid $panel; padding: 0; }
    #composer:focus-within { border-top: solid $primary; }
    #composer-input { color: $text; }
    #composer-input .text-area--placeholder { color: $text-muted; }
    #composer-input .text-area--cursor { background: $primary; color: $background; }
    #prompt-glyph, #prompt-glyph.-default, #prompt-glyph.-plan, #prompt-glyph.-acceptEdits, #prompt-glyph.-bypass { color: $primary; }
    #composer-actions { height: 1; width: 1fr; }
    #composer-help { width: 1fr; height: 1; color: $text-muted; }
    #command-hint { margin: 0; color: $text-muted; height: auto; }
    #slash-command-menu { border: none; background: $surface; max-height: 7; margin: 0; }
    .slash-command-item.-active { background: $foreground; color: $background; }
    #approval-region { height: auto; max-height: 12; display: none; }
    #inline-permission-panel { height: auto; max-height: 12; background: $surface; border-left: solid $warning; padding: 0 1; }
    #inline-permission-panel:focus { border-left: solid $warning; }
    #permission-title { color: $warning; margin: 0; }
    #permission-command, #permission-details, #permission-description, #permission-prompt, .permission-preview { color: $text; margin: 0; }
    #permission-cwd, #permission-description { color: $text-muted; height: auto; }
    .permission-preview.-error { color: $error; }
    .permission-preview.-success { color: $success; }
    .permission-option.-active { background: $foreground; color: $background; }
    #permission-help { color: $text-muted; margin: 0; }
    #pending-queue { display: none; height: auto; max-height: 8; overflow-y: auto; background: $surface; }
    .queue-heading, .queue-actions { height: 1; }
    .queue-heading Static { width: 1fr; height: 1; }
    .queue-item { height: auto; padding: 0 1; margin-bottom: 1; }
    .queue-text { height: auto; max-height: 2; }
    #plan-panel { display: none; height: auto; max-height: 6; color: $text-muted; }
    #plan-preview { height: auto; max-height: 4; }
    .plan-actions { height: 1; }
    #plan-execute { color: $primary; text-style: bold; }
    #plan-execute:focus { color: $background; }
    #status-bar { margin: 0 2 1 2; height: 1; color: $text-muted; background: $surface; }
    #status-left, #status-left.-plan, #status-left.-acceptEdits, #status-left.-bypass { color: $text-muted; width: 1fr; }
    #status-center { width: auto; max-width: 35%; }
    #status-right { width: auto; margin-left: 1; color: $text-muted; }
    #status-details { display: none; height: auto; max-height: 8; margin: 0 2; color: $text-muted; }
    Screen.-narrow #phase-explanation { display: none; }
    Screen.-narrow #status-center { display: none; }
    Screen.-narrow #status-bar { height: 2; }
    Screen.-narrow #status-left { height: 2; }
    Screen.-tiny #status-bar, Screen.-tiny #status-left { height: 3; }
    Screen.-tiny #composer-actions.-working #chat-model,
    Screen.-tiny #composer-actions.-working #chat-policy,
    Screen.-tiny #composer-actions.-working #chat-details { display: none; }
    Screen.-narrow #status-right { display: none; }
    Screen.-narrow #composer-help { display: none; }
    Screen.-narrow #banner { margin-top: 0; }
    """

    BINDINGS = [
        ("ctrl+c", "request_quit", "取消/退出"),
        ("ctrl+d", "toggle_details", "状态详情"),
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
        apply_theme(self, getattr(ui_config, "theme", "dark"))
        self._slash_command_registry = slash_command_registry or create_default_slash_command_registry()
        self._is_streaming = False
        self._chat_started = False
        self._message_widgets: dict[str, MessageWidget] = {}
        self._status_hint = "就绪"
        self._slash_menu_matches: list[SlashCompletionCandidate] = []
        self._slash_menu_index = 0
        self._pending_permissions: dict[str, PermissionRequest] = {}
        self._permission_ui_lock = asyncio.Lock()
        self._turn_succeeded = False
        self._details_open = False
        self._mcp_manager = mcp_manager
        self._tool_registry = tool_registry
        self._settings_service = settings_service
        self.mcp_initialization_complete = mcp_manager is None or not mcp_manager.has_servers

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield BannerWidget(Path.cwd())
            yield StageBar()
            yield VerticalScroll(id="chat-view")
            with Vertical(id="composer-region"):
                yield PlanPanel()
                yield PendingQueue()
                yield Vertical(id="approval-region")
                yield SlashCommandMenu()
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
                with Horizontal(id="composer-actions"):
                    yield Static("Enter 发送 · Shift+Enter 换行", id="composer-help", markup=False)
                    yield Button("模型", id="chat-model", classes="quiet-action")
                    yield Button("审批", id="chat-policy", classes="quiet-action")
                    yield Button("详情", id="chat-details", classes="quiet-action")
                    yield Button("补充", id="chat-steer", classes="quiet-action")
                    yield Button("排队", id="chat-queue", classes="quiet-action")
            with Horizontal(id="status-bar"):
                yield Static(id="status-left", markup=False)
                yield Static(id="status-center")
                yield Static(id="status-right")
            yield Static(id="status-details", markup=False)

    async def on_mount(self) -> None:
        self.screen.set_class(self.size.width < 64, "-narrow")
        composer = self.query_one(ComposerTextArea)
        composer.disabled = not self.mcp_initialization_complete
        if self.mcp_initialization_complete:
            composer.focus()
        self._update_composer_height()
        self._refresh_mode_chrome()
        self._refresh_composer_placeholder()
        await self._refresh_command_ui()
        self._refresh_status_bar()
        self._refresh_context_usage()
        await self._refresh_pending_queue()
        self._refresh_plan_panel()
        if self._mcp_manager is not None:
            self._mcp_manager.add_progress_callback(self._handle_mcp_progress)
            if self._mcp_manager.has_servers:
                self._status_hint = "正在连接 MCP"
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
            self._status_hint = "就绪"
            self._refresh_composer_placeholder()
            self._refresh_status_bar()
            composer.focus()

    def _handle_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        self.query_one(BannerWidget).update_mcp_progress(progress)
        if progress.state == "complete":
            self._refresh_context_usage()

    def on_resize(self) -> None:
        self.screen.set_class(self.size.width < 64, "-narrow")
        self.screen.set_class(self.size.width < 48, "-tiny")
        self.call_after_refresh(self._refresh_status_bar)
        self.call_after_refresh(self._update_composer_height)
        self.call_after_refresh(self._fit_chat_panels)

    async def action_request_quit(self) -> None:
        if self._is_streaming:
            if self._turn_runner.cancel_active_turn():
                self._status_hint = "正在停止 · 草稿和队列会保留"
                self._refresh_status_bar()
            return
        self.exit(0)

    @on(TextArea.Changed, "#composer-input")
    async def handle_composer_changed(self) -> None:
        self._update_composer_height()
        await self._refresh_command_ui()

    @on(SlashMenuNavigateRequested)
    async def handle_slash_menu_navigation(self, event: SlashMenuNavigateRequested) -> None:
        await self._move_slash_menu(event.direction)

    @on(SlashMenuAcceptRequested)
    async def handle_slash_menu_accept(self) -> None:
        await self._accept_slash_menu_selection()

    @on(SlashMenuDismissRequested)
    async def handle_slash_menu_dismiss(self) -> None:
        await self._dismiss_slash_menu()

    @on(SlashCommandChosen)
    async def handle_slash_command_chosen(self, event: SlashCommandChosen) -> None:
        await self._accept_completion(event.candidate_key)

    @on(PermissionModeCycleRequested)
    async def handle_permission_mode_cycle_requested(self) -> None:
        if self._is_streaming:
            return
        phases = ("discuss", "plan", "execute")
        phase = self._session_controller.work_phase
        self._apply_turn_event(self._turn_runner.set_phase(phases[(phases.index(phase) + 1) % len(phases)]))
        await self._refresh_command_ui()
        self._refresh_status_bar()
        self.query_one("#composer-input", ComposerTextArea).focus()

    @on(ComposerSubmitted)
    async def handle_input_submitted(self, event: ComposerSubmitted) -> None:
        if not self.mcp_initialization_complete:
            return
        text = event.value.strip()
        if not text:
            return

        if self._is_streaming:
            if text.startswith("/"):
                self._status_hint = "当前任务结束后可使用命令 · 输入已保留"
                self._refresh_status_bar()
                return
            delivery = event.delivery or getattr(self._ui_config, "busy_enter_action", "follow_up")
            if delivery == "draft":
                self._status_hint = "草稿已保留 · 可点击补充或排队"
                self._refresh_status_bar()
                return
            try:
                self._turn_runner.enqueue_input(text, delivery=delivery)
            except (LanCherError, ValueError, RuntimeError) as exc:
                self.notify(str(exc), severity="warning")
                return
            event.composer.clear()
            await self._refresh_command_ui()
            await self._refresh_pending_queue()
            return

        slash_match = self._slash_command_registry.parse_submission(
            text,
            self._session_controller.runtime_mode,
        )
        if slash_match is not None:
            payload = await self._execute_slash_command(slash_match.definition.name, slash_match.arguments_text)
            event.composer.clear()
            await self._refresh_command_ui()
            if payload is None:
                return
            text = payload
        else:
            event.composer.clear()
            await self._refresh_command_ui()

        self._begin_turn(text)

    def _begin_turn(self, text: str, *, queued: bool = False) -> None:
        if self._is_streaming:
            return

        if not self._chat_started:
            self._chat_started = True
            self.query_one(BannerWidget).set_compact(True)
            self.query_one("#chat-view", VerticalScroll).set_class(True, "-banner-collapsed")

        self._is_streaming = True
        self._turn_succeeded = False
        self._status_hint = "正在处理"
        self._refresh_composer_placeholder()
        self._refresh_status_bar()
        self._refresh_plan_panel()
        self.process_prompt(text, queued=queued)

    @on(InlinePermissionPanel.Resolved)
    async def handle_permission_resolved(self, event: InlinePermissionPanel.Resolved) -> None:
        event.stop()
        request_id = event.resolution.request_id
        if request_id not in self._pending_permissions:
            return
        self._turn_runner.resolve_permission_request(event.resolution)
        await self._close_inline_permission(request_id)

    @work(exclusive=False, exit_on_error=False)
    async def process_prompt(self, text: str, *, queued: bool = False) -> None:
        try:
            stream = self._turn_runner.run_next_queued_turn() if queued else self._turn_runner.run_user_turn(text)
            # 消费事件期间也可能关闭界面；显式关闭生成器，等待执行器清理。
            async with aclosing(stream):
                async for event in stream:
                    await self._consume_turn_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "event=tui_turn_worker_failed exception_type=%s", type(exc).__name__
            )
            self._turn_runner.pause_queue()
            self._status_hint = "未完成 · 队列已暂停"
            self.notify(str(exc), title="本轮未完成", severity="error")
        finally:
            self._is_streaming = False
            if self.is_running:
                await self._finish_turn_view()
            else:
                self._pending_permissions.clear()
                self._session_controller.auto_save()

    async def on_unmount(self) -> None:
        # Textual 取消 worker 后不会等待所有后台执行器；退出前显式完成收尾。
        await self._turn_runner.stop_and_wait()

    async def _finish_turn_view(self) -> None:
        for request_id in list(self._pending_permissions):
            await self._close_inline_permission(request_id)
        auto_save_error = self._session_controller.auto_save()
        if auto_save_error:
            self.notify(auto_save_error, title="会话自动保存", severity="error", timeout=10)
        if self._turn_succeeded:
            self._status_hint = "已完成"
        input_widget = self.query_one("#composer-input", ComposerTextArea)
        input_widget.disabled = False
        if len(self.screen_stack) == 1:
            input_widget.focus()
        self._refresh_mode_chrome()
        self._refresh_composer_placeholder()
        await self._refresh_command_ui()
        self._refresh_status_bar()
        self._refresh_plan_panel()
        await self._refresh_pending_queue()
        if self._turn_succeeded:
            self.call_later(self._start_next_queued)

    def _start_next_queued(self) -> None:
        if self._is_streaming or self._turn_runner.has_active_turn or self._turn_runner.queue_paused:
            return
        items = self._turn_runner.pending_inputs
        if items and items[0].state == "pending" and items[0].delivery == "follow_up":
            self._begin_turn("", queued=True)

    def _refresh_status_bar(self) -> None:
        usage = self._session_controller.total_usage()
        center_text = self._status_hint or ("正在处理" if self._is_streaming else "就绪")
        status_left = self.query_one("#status-left", Static)
        status_center = self.query_one("#status-center", Static)
        status_right = self.query_one("#status-right", Static)

        banner = self.query_one(BannerWidget)
        estimate = banner._context_usage_status.replace("上下文 ", "预计 ")
        action = getattr(self._ui_config, "busy_enter_action", "follow_up")
        enter_action = {"follow_up": "排队", "steer": "补充", "draft": "草稿"}[action] if self._is_streaming else "发送"
        compact_state = center_text.split(" · ", 1)[0]
        if self._pending_permissions:
            compact_state = "等待确认"
        elif self._turn_runner.queue_paused:
            compact_state = "队列暂停"
        elif self._is_streaming:
            compact_state = "正在停止" if "停止" in compact_state else "处理中"
        compact_state = compact_state[:6]
        label = self._status_left_text()
        model, phase, policy = label.rsplit(" · ", 2)
        if self.size.width < 64:
            available = max(8, self.size.width - 4)
            model_limit = max(5, available - (10 if self.size.width < 48 else 28))
            model_text = Text(model)
            model_text.truncate(model_limit, overflow="ellipsis")
            model = model_text.plain
            if self.size.width < 48:
                label = f"{model} · {phase}\n{policy} · {estimate}\n{compact_state} · Enter {enter_action}"
            else:
                label = f"{model} · {phase} · {policy}\n{estimate} · {compact_state} · Enter {enter_action}"
        else:
            # 模型名按终端格宽截断，给阶段与权限保留位置。
            # 使用本轮状态的宽度，不能沿用状态变化前上一帧的布局。
            hud_width = max(1, min(self.size.width, 112) - 4)
            center_width = min(cell_len(f"{estimate} · {center_text}"), (hud_width * 35 + 99) // 100)
            available = hud_width - center_width - cell_len("Ctrl+D 详情") - 1
            model_text = Text(model)
            model_text.truncate(max(1, available - cell_len(f" · {phase} · {policy}")), overflow="ellipsis")
            model = model_text.plain
            label = f"{model} · {phase} · {policy}"
        colors = theme_palette(self.theme)
        summary = Text(label, style=colors["muted"])
        summary.stylize("bold " + colors["text"], 0, len(model))
        phase_start = len(model) + 3
        summary.stylize(colors["primary"], phase_start, phase_start + len(phase))
        status_left.update(summary)
        for candidate in MODE_SEQUENCE:
            status_left.set_class(candidate != "default" and candidate == self._session_controller.runtime_mode, f"-{candidate}")

        status_center.update(f"{estimate} · {center_text}")
        status_right.update("Ctrl+D 详情")
        self.query_one(StageBar).update_phase(self._session_controller.work_phase, busy=self._is_streaming)
        config = getattr(self._turn_runner, "model_config", None)
        ref = getattr(self._turn_runner, "model_ref", None)
        details = (
            f"本次模型：{self._status_left_text()}\n"
            f"工作目录：{self._session_controller._cwd}\n"
            f"模型引用：{ref or self._provider_config.model}\n"
            f"新对话默认：{getattr(config, 'default_model', None) or '当前配置'}\n"
            f"{self._format_usage_text(usage)} · {banner._context_usage_status}\n"
            f"{banner._mcp_status}"
        )
        self.query_one("#status-details", Static).update(details)
        for button_id in ("chat-model", "chat-policy"):
            self.query_one(f"#{button_id}", Button).disabled = self._is_streaming
        self.query_one("#chat-steer", Button).display = self._is_streaming
        self.query_one("#chat-queue", Button).display = self._is_streaming
        self.query_one("#composer-actions").set_class(self._is_streaming, "-working")
        busy_help = {"follow_up": "Enter 排下一轮", "steer": "Enter 补充当前任务", "draft": "Enter 保留草稿"}[action]
        self.query_one("#composer-help", Static).update(
            busy_help + " · Ctrl+Enter 补充 · Ctrl+C 停止" if self._is_streaming else "Enter 发送 · Shift+Enter 换行"
        )

    def _refresh_context_usage(self) -> None:
        banner = self.query_one(BannerWidget)
        try:
            visible_tools = []
            deferred_tool_groups = []
            if self._tool_registry is not None:
                visible_tools = self._tool_registry.list_definitions(
                    discovered_names=set(),
                    mode=self._session_controller.runtime_mode,
                    work_phase=self._session_controller.work_phase,
                )
                deferred_tool_groups = self._tool_registry.list_deferred_index(
                    mode=self._session_controller.runtime_mode,
                    work_phase=self._session_controller.work_phase,
                )
            request = self._session_controller.build_request(
                visible_tools,
                allow_tool_calls=True,
                mode=self._session_controller.runtime_mode,
                work_phase=self._session_controller.work_phase,
                permission_policy=self._session_controller.permission_policy,
                deferred_tool_groups=deferred_tool_groups,
            )
            used_tokens = self._session_controller.estimate_request_tokens(request)
        except Exception:
            logger.exception("event=tui_context_usage_estimate_failed")
            banner.update_context_usage(None, self._session_controller.context_window)
            self._refresh_status_bar()
            return
        banner.update_context_usage(used_tokens, self._session_controller.context_window)
        self._refresh_status_bar()

    def _status_left_text(self) -> str:
        config = getattr(self._turn_runner, "model_config", None)
        if config is not None and self._turn_runner.model_ref:
            model = model_display_name(config, self._turn_runner.model_ref)
        else:
            model = self._provider_config.model
        policy = {"default": "逐次确认", "acceptEdits": "自动编辑", "bypass": "跳过询问"}[self._session_controller.permission_policy]
        phase = {"discuss": "讨论", "plan": "计划", "execute": "执行"}[self._session_controller.work_phase]
        return f"{model} · {phase} · {policy}"

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
        chat_view = self.query_one("#chat-view", VerticalScroll)
        follow_bottom = chat_view.is_vertical_scroll_end
        self._apply_turn_event(event)
        if event.message is not None and event.kind in {"user_message_created", "assistant_message_started"}:
            await self._mount_message_widget(event.message)
        elif event.message is not None:
            self._sync_message_widget(event.message.id)
        if event.kind == "permission_request_created" and event.permission_request is not None:
            await self._request_inline_permission(event.permission_request)
        if event.kind in {"permission_request_closed", "permission_request_resolved"}:
            request = event.permission_request
            resolution = event.permission_resolution
            request_id = request.request_id if request else resolution.request_id if resolution else None
            if request_id:
                await self._close_inline_permission(request_id)
        if event.kind in {"pending_input_changed", "steering_applied", "turn_completed", "turn_cancelled", "turn_failed"}:
            await self._refresh_pending_queue()
        if follow_bottom:
            self.call_after_refresh(chat_view.scroll_end, animate=False)
        self._refresh_status_bar()
        self._refresh_plan_panel()
        if event.kind in CONTEXT_REFRESH_EVENTS:
            self._refresh_context_usage()

    async def _request_inline_permission(self, request: PermissionRequest) -> None:
        async with self._permission_ui_lock:
            self._pending_permissions[request.request_id] = request
            await self._show_next_permission()
        self._fit_chat_panels()

    async def _show_next_permission(self) -> None:
        region = self.query_one("#approval-region", Vertical)
        region.display = bool(self._pending_permissions)
        if region.children or not self._pending_permissions:
            return
        request = next(iter(self._pending_permissions.values()))
        panel = InlinePermissionPanel(request)
        await region.mount(panel)
        panel.focus()

    async def _close_inline_permission(self, request_id: str) -> None:
        async with self._permission_ui_lock:
            self._pending_permissions.pop(request_id, None)
            for panel in list(self.query(InlinePermissionPanel)):
                if panel.request.request_id == request_id:
                    await panel.close_request_screen()
                    await panel.remove()
            await self._show_next_permission()
        self._fit_chat_panels()
        if not self._pending_permissions and len(self.screen_stack) == 1:
            self.query_one(ComposerTextArea).focus()

    def _apply_turn_event(self, event: TurnEvent) -> None:
        if event.kind in {"mode_changed", "phase_changed", "policy_changed"}:
            self._status_hint = event.progress_message or "阶段已更新"
            self._refresh_mode_chrome()
            self._refresh_composer_placeholder()
            return
        if event.kind == "progress_updated":
            self._status_hint = event.progress_message or ("正在处理" if self._is_streaming else "就绪")
            return
        if event.kind == "turn_cancelled":
            self._status_hint = event.progress_message or "已停止"
            return
        if event.kind == "turn_failed":
            self._status_hint = event.error_text or "未完成"
            return
        if event.kind == "turn_completed":
            self._turn_succeeded = True
            self._status_hint = "已完成"
            return
        if event.kind == "permission_request_created":
            self._status_hint = "等待确认 · 可继续编辑草稿"
            return
        if event.kind == "permission_request_resolved":
            self._status_hint = "正在处理"
            return
        if event.kind in {"assistant_text_delta", "tool_call_started", "tool_result_received", "usage_updated"}:
            self._status_hint = "正在处理"

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
        if self._is_streaming:
            composer.placeholder = "任务进行中，仍可输入下一条消息"
            return
        if self._session_controller.work_phase == "discuss":
            composer.placeholder = "讨论想法或调查代码，此阶段不修改文件"
            return
        if self._session_controller.work_phase == "plan":
            composer.placeholder = PLAN_PLACEHOLDER
            return
        composer.placeholder = DEFAULT_PLACEHOLDER

    def action_toggle_details(self) -> None:
        if self.size.height < 24:
            self.push_screen(ReadOnlyDetailsScreen(str(self.query_one("#status-details", Static).render())))
            return
        self._details_open = not self._details_open
        self.query_one("#status-details", Static).display = self._details_open

    async def _refresh_pending_queue(self) -> None:
        await self.query_one(PendingQueue).update_items(
            self._turn_runner.pending_inputs,
            paused=self._turn_runner.queue_paused,
            busy=self._is_streaming,
        )
        self._fit_chat_panels()

    def _refresh_plan_panel(self) -> None:
        self.query_one(PlanPanel).update_snapshot(
            self._session_controller.plan_snapshot,
            busy=self._is_streaming,
            phase=self._session_controller.work_phase,
        )
        if self.size.height < 24 and self._pending_permissions:
            self.query_one(PlanPanel).display = False
        self._fit_chat_panels()

    def _fit_chat_panels(self) -> None:
        """小终端先给输入和 HUD 留出位置，其余区域在自己的视口内滚动。"""
        if not self.is_mounted:
            return
        short = self.size.height < 24
        approval = self.query_one("#approval-region", Vertical)
        queue = self.query_one(PendingQueue)
        plan = self.query_one(PlanPanel)
        if not short:
            self.query_one(BannerWidget).styles.height = "auto"
            self.query_one(BannerWidget).styles.margin = (0 if self._chat_started else 1, 2, 0, 2)
            self.query_one("#chat-view").styles.margin = (0 if self._chat_started else 1, 1, 0, 1)
            approval.styles.max_height = 12
            approval.styles.height = "auto"
            queue.styles.max_height = 8
            plan.styles.max_height = 6
            self.query_one("#plan-preview", Static).styles.max_height = 4
            for panel in self.query(InlinePermissionPanel):
                panel.set_compact(False)
                panel.styles.height = "auto"
                panel.styles.max_height = 12
            return
        self._details_open = False
        self.query_one("#status-details", Static).display = False
        self.query_one(BannerWidget).styles.height = 1
        self.query_one(BannerWidget).styles.margin = (0, 2, 0, 2)
        self.query_one("#chat-view").styles.margin = (0, 1, 0, 1)
        self.query_one(CommandHintBar).styles.max_height = 1
        composer = self.query_one(ComposerTextArea)
        line_limit = 1 if self._pending_permissions else 3
        lines = max(1, min(line_limit, composer.wrapped_document.height))
        composer.styles.height = lines
        self.query_one("#composer").styles.height = lines + 1
        # 预留横幅、阶段、聊天、完整 HUD、输入边框与操作行。
        hud_extra = 1 if self.size.width < 48 else 0
        budget = max(3, self.size.height - 8 - lines - hud_extra)
        queue_height = min(3, budget) if queue.display else 0
        queue.styles.max_height = max(1, queue_height)
        budget -= queue_height
        plan.styles.max_height = 2
        self.query_one("#plan-preview", Static).styles.max_height = 1
        if plan.display:
            budget -= 2
        permission_height = 3
        approval.styles.height = permission_height if self._pending_permissions else "auto"
        approval.styles.max_height = permission_height
        for panel in self.query(InlinePermissionPanel):
            panel.set_compact(True)
            panel.styles.height = permission_height
            panel.styles.max_height = permission_height

    @on(Button.Pressed, "#composer-actions Button")
    async def handle_composer_button(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "chat-model":
            await self._execute_slash_command("model", "")
        elif button_id == "chat-policy":
            self.push_screen(PermissionPolicyScreen(), self._handle_policy_selected)
        elif button_id == "chat-details":
            self.action_toggle_details()
        elif button_id in {"chat-steer", "chat-queue"}:
            composer = self.query_one(ComposerTextArea)
            await self.handle_input_submitted(ComposerSubmitted(composer, composer.text, "steer" if button_id == "chat-steer" else "follow_up"))

    @on(ChatAction)
    async def handle_chat_action(self, event: ChatAction) -> None:
        event.stop()
        try:
            if event.action == "phase":
                if not self._is_streaming:
                    self._apply_turn_event(self._turn_runner.set_phase(event.value))
                    self._refresh_status_bar()
                    self._refresh_plan_panel()
                    await self._refresh_command_ui()
            elif event.action == "focus-composer":
                self.query_one(ComposerTextArea).focus()
            elif event.action == "queue-toggle":
                if self._turn_runner.queue_paused:
                    self._turn_runner.resume_queue()
                    self._start_next_queued()
                else:
                    self._turn_runner.pause_queue()
            elif event.action == "queue-delete":
                self._turn_runner.remove_pending_input(event.value)
            elif event.action == "queue-convert":
                item = next((item for item in self._turn_runner.pending_inputs if item.id == event.value), None)
                if item is not None:
                    self._turn_runner.convert_pending_input(item.id, "follow_up" if item.delivery == "steer" else "steer")
            elif event.action == "queue-edit":
                item = next((item for item in self._turn_runner.pending_inputs if item.id == event.value), None)
                if item is not None:
                    self._turn_runner.pause_queue()
                    self.push_screen(PendingInputEditor(item.text), lambda text: self._save_pending_edit(item.id, text))
            elif event.action in {"plan-review", "plan-execute"}:
                snapshot = self._session_controller.plan_snapshot
                if snapshot is not None:
                    session_id = self._session_controller.session_id
                    if event.action == "plan-review":
                        self.push_screen(PlanReviewScreen(session_id, snapshot, can_execute=snapshot.ready and not self._is_streaming), self._handle_plan_accepted)
                    else:
                        self._handle_plan_accepted((session_id, snapshot.digest))
        except (LanCherError, ValueError, RuntimeError) as exc:
            self.notify(str(exc), title="未能完成操作", severity="warning")
        await self._refresh_pending_queue()

    def _save_pending_edit(self, item_id: str, text: str | None) -> None:
        if text is not None:
            try:
                self._turn_runner.update_pending_input(item_id, text)
            except (LanCherError, ValueError, RuntimeError) as exc:
                self.notify(str(exc), severity="warning")
            else:
                self.notify("已保存，队列保持暂停；准备好后点击继续队列。", title="待发送内容")
        self.call_later(self._refresh_pending_queue)

    def _handle_plan_accepted(self, value: tuple[str, str] | None) -> None:
        if value is None:
            return
        try:
            text = self._turn_runner.prepare_plan_execution(*value)
        except (LanCherError, ValueError, RuntimeError) as exc:
            self.notify(str(exc), title="计划需要重新确认", severity="warning")
            return
        self._refresh_mode_chrome()
        self._begin_turn(text)

    def _handle_policy_selected(self, policy: str | None) -> None:
        if policy is not None:
            try:
                self._apply_turn_event(self._turn_runner.set_permission_policy(policy))
            except (LanCherError, ValueError, RuntimeError) as exc:
                self.notify(str(exc), severity="warning")
        self._refresh_status_bar()
        self.query_one(ComposerTextArea).focus()

    def _apply_ui_settings(self, ui_config: UIConfig) -> None:
        self._ui_config = ui_config
        apply_theme(self, getattr(ui_config, "theme", "dark"))
        for widget in self._message_widgets.values():
            widget._show_thinking = ui_config.show_thinking_status
            widget._sync_view()
        self.query_one(BannerWidget).refresh()
        self._refresh_status_bar()

    def _apply_models_settings(self, config) -> str | None:
        self._turn_runner.reload_models(config)
        self._refresh_status_bar()
        self._refresh_context_usage()
        return self._turn_runner.model_ref

    async def _refresh_command_ui(self) -> None:
        composer = self.query_one("#composer-input", ComposerTextArea)
        menu = self.query_one(SlashCommandMenu)
        hint_bar = self.query_one(CommandHintBar)
        composer.clear_accepted_slash_command_if_needed()
        if self._is_streaming:
            self._slash_menu_matches = []
            self._slash_menu_index = 0
            composer.slash_menu_active = False
            await menu.set_candidates([], None)
            hint_bar.set_hint("")
            return

        cursor_at_end = composer.cursor_location == composer.document.end
        sessions = self._session_controller.list_saved_sessions() if cursor_at_end else []
        matches = (
            self._slash_command_registry.complete(
                SlashCompletionContext(
                    text=composer.text,
                    mode=self._session_controller.runtime_mode,
                    session_names=tuple(item.name for item in sessions),
                    active_session_name=self._session_controller.active_session_name,
                    model_choices=self._model_completion_choices(),
                    active_model_ref=getattr(self._turn_runner, "model_ref", None),
                )
            )
            if cursor_at_end and not composer.should_suppress_slash_menu()
            else []
        )
        active_key = self._current_active_completion_key()
        match_keys = [candidate.key for candidate in matches]
        if active_key in match_keys:
            self._slash_menu_index = match_keys.index(active_key)
        else:
            self._slash_menu_index = 0
        self._slash_menu_matches = matches
        active_key = self._current_active_completion_key()
        await menu.set_candidates(matches, active_key)
        composer.slash_menu_active = bool(matches)
        if matches and active_key is not None:
            active = matches[self._slash_menu_index]
            hint_bar.set_hint(active.description)
            return

        self._slash_menu_matches = []
        self._slash_menu_index = 0
        composer.slash_menu_active = False

        command_name = extract_exact_command_name(composer.text)
        if command_name is not None:
            command = self._slash_command_registry.get(command_name)
            if command is not None:
                hint_bar.set_hint(command.hint_text)
                return

        hint_bar.set_hint(DEFAULT_COMMAND_HINT)

    async def _move_slash_menu(self, direction: int) -> None:
        if not self._slash_menu_matches:
            return
        self._slash_menu_index = (self._slash_menu_index + direction) % len(self._slash_menu_matches)
        menu = self.query_one(SlashCommandMenu)
        await menu.set_candidates(
            self._slash_menu_matches,
            self._current_active_completion_key(),
        )

    async def _accept_slash_menu_selection(self) -> None:
        candidate_key = self._current_active_completion_key()
        if candidate_key is None:
            return
        await self._accept_completion(candidate_key)

    async def _accept_completion(self, candidate_key: str) -> None:
        candidate = next(
            (item for item in self._slash_menu_matches if item.key == candidate_key),
            None,
        )
        if candidate is None:
            return

        composer = self.query_one("#composer-input", ComposerTextArea)
        composer.text = candidate.apply(composer.text)
        composer.cursor_location = composer.document.end
        if candidate.append_space:
            composer.remember_accepted_slash_command("")
        else:
            composer.remember_accepted_slash_command(composer.text)
        composer.focus()
        await self._refresh_command_ui()

    async def _dismiss_slash_menu(self) -> None:
        self._slash_menu_matches = []
        self._slash_menu_index = 0
        composer = self.query_one("#composer-input", ComposerTextArea)
        composer.slash_menu_active = False
        await self.query_one(SlashCommandMenu).set_candidates([], None)

        command_name = extract_exact_command_name(composer.text)
        if command_name is not None:
            command = self._slash_command_registry.get(command_name)
            if command is not None:
                self.query_one(CommandHintBar).set_hint(command.hint_text)
                return
        self.query_one(CommandHintBar).set_hint(DEFAULT_COMMAND_HINT)

    def _current_active_completion_key(self) -> str | None:
        if not self._slash_menu_matches:
            return None
        if self._slash_menu_index >= len(self._slash_menu_matches):
            self._slash_menu_index = 0
        return self._slash_menu_matches[self._slash_menu_index].key

    async def _execute_slash_command(self, command_name: str, arguments_text: str) -> str | None:
        if command_name == "exit":
            self.exit(0)
            return None

        if command_name == "do":
            self._apply_turn_event(self._turn_runner.set_phase("execute"))
            self._refresh_status_bar()
            self._refresh_context_usage()
            self._refresh_plan_panel()
            return arguments_text.strip() or None

        if command_name in {"plan", "discuss"}:
            self._apply_turn_event(self._turn_runner.set_phase(command_name))
            self._refresh_status_bar()
            self._refresh_context_usage()
            self._refresh_plan_panel()
            payload = arguments_text.strip()
            if not payload:
                return None
            return payload

        if command_name == "status":
            self.action_toggle_details()
            return None

        if command_name == "permissions":
            self.push_screen(PermissionPolicyScreen(), self._handle_policy_selected)
            return None

        if command_name == "mode":
            requested_mode = arguments_text.strip()
            if requested_mode not in set(MODE_SEQUENCE):
                self._status_hint = "未知模式"
                self._refresh_status_bar()
                return None
            self._apply_turn_event(self._turn_runner.set_mode(requested_mode))  # type: ignore[arg-type]
            self._refresh_status_bar()
            self._refresh_context_usage()
            return None

        if command_name == "model":
            if self._is_streaming or self._turn_runner.has_active_turn:
                self.notify("请等待当前轮次结束后再切换模型。", title="模型", severity="warning")
                return None
            config = getattr(self._turn_runner, "model_config", None)
            if config is None:
                self.notify("模型目录尚未加载。", title="模型", severity="warning")
                return None
            ref = arguments_text.strip()
            if ref:
                self._handle_model_selected(ref)
            else:
                self.push_screen(ModelPickerScreen(config, self._turn_runner.model_ref), self._handle_model_selected)
            return None

        if command_name == "settings":
            if self._settings_service is None:
                self._status_hint = "设置暂不可用"
                self._refresh_status_bar()
                return None
            self.push_screen(SettingsScreen(
                self._settings_service,
                current_model_ref=getattr(self._turn_runner, "model_ref", None),
                on_models_saved=self._apply_models_settings,
                on_model_selected=self._handle_model_selected,
                on_ui_saved=self._apply_ui_settings,
            ), self._handle_settings_result)
            return None

        if command_name == "compact":
            if arguments_text.strip():
                self.notify("用法：/compact", title="上下文压缩", severity="warning")
                return None
            self._is_streaming = True
            self._status_hint = "正在压缩上下文..."
            composer = self.query_one("#composer-input", ComposerTextArea)
            composer.disabled = True
            self._refresh_status_bar()
            self.notify("正在压缩上下文...", title="上下文压缩")
            try:
                result = await self._turn_runner.compact_context()
            except Exception as exc:
                self.notify(str(exc), title="上下文压缩失败", severity="error", timeout=10)
            else:
                self.notify(
                    f"已压缩，token 从 {result.before_tokens} 降至 {result.after_tokens}",
                    title="上下文压缩",
                    timeout=10,
                )
            finally:
                self._is_streaming = False
                self._status_hint = "就绪"
                composer.disabled = False
                composer.focus()
                self._refresh_status_bar()
                self._refresh_context_usage()
            return None

        if command_name == "session":
            await self._execute_session_command(arguments_text)
            return None

        return None

    async def _execute_session_command(self, arguments_text: str) -> None:
        arguments = arguments_text.split()
        if not arguments:
            self.notify(
                "用法：/session <list|save|remove|rename|resume> [名称]",
                title="Session",
                severity="warning",
            )
            return

        action = arguments[0]
        try:
            if action == "list" and len(arguments) == 1:
                sessions = self._session_controller.list_saved_sessions()
                if not sessions:
                    message = "当前项目还没有已保存的会话。"
                else:
                    active = self._session_controller.active_session_name
                    message = "\n".join(
                        f"{'* ' if item.name == active else '  '}{item.name} · "
                        f"{item.updated_at.astimezone().strftime('%Y-%m-%d %H:%M')} · "
                        f"{item.message_count} 条消息 · {item.permission_rule_count} 条会话权限"
                        for item in sessions
                    )
                self.notify(message, title="项目会话", timeout=10)
                return

            if action == "save" and len(arguments) == 2:
                self._session_controller.save_session(arguments[1])
                self.notify(f"已保存并绑定会话：{arguments[1]}", title="Session")
                return

            if action == "remove" and len(arguments) == 2:
                self._session_controller.remove_session(arguments[1])
                self.notify(f"已删除会话：{arguments[1]}", title="Session")
                return

            if action == "rename" and len(arguments) == 3:
                self._session_controller.rename_session(arguments[1], arguments[2])
                self.notify(f"已将 {arguments[1]} 重命名为 {arguments[2]}", title="Session")
                return

            if action == "resume" and len(arguments) in {2, 3}:
                force = len(arguments) == 3 and arguments[2] == "--force"
                if len(arguments) == 3 and not force:
                    raise SessionStoreError("resume 的第三个参数只能是 --force。")
                if getattr(self._turn_runner, "model_config", None) is not None:
                    permission_count = self._turn_runner.resume_session(arguments[1], force=force)
                else:
                    permission_count = self._session_controller.resume_session(arguments[1], force=force)
                await self._restore_session_view()
                notice = getattr(self._turn_runner, "model_notice", "")
                self.notify(
                    f"已恢复会话：{arguments[1]}（恢复 {permission_count} 条会话权限）" + (f"\n{notice}" if notice else ""),
                    title="Session",
                )
                return

            raise SessionStoreError("参数不正确，请查看 /session 的命令提示。")
        except (SessionStoreError, LanCherError, ValueError, RuntimeError) as exc:
            self.notify(str(exc), title="Session", severity="error", timeout=10)

    async def _restore_session_view(self) -> None:
        chat_view = self.query_one("#chat-view", VerticalScroll)
        for child in list(chat_view.children):
            await child.remove()
        self._message_widgets.clear()
        for message in self._session_controller.state.messages:
            await self._mount_message_widget(message)

        self._chat_started = bool(self._session_controller.state.messages)
        self.query_one(BannerWidget).set_compact(self._chat_started)
        chat_view.set_class(self._chat_started, "-banner-collapsed")
        self._refresh_mode_chrome()
        self._refresh_composer_placeholder()
        self._refresh_status_bar()
        self._refresh_context_usage()
        self._refresh_plan_panel()
        await self._refresh_pending_queue()
        chat_view.scroll_end(animate=False)

    def _handle_settings_result(self, result: SettingsResult | None) -> None:
        if result is not None and result.saved:
            try:
                if result.config is not None and not getattr(result, "runtime_applied", False):
                    fallback = self._turn_runner.reload_models(result.config)
                    if fallback:
                        self.notify(self._turn_runner.model_notice, title="模型")
                if result.config is not None:
                    self._apply_ui_settings(result.config.ui)
            except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                self.notify(f"设置已保存，当前模型更新失败：{exc}", title="模型", severity="error", timeout=10)
                self._status_hint = "模型更新失败"
            else:
                self._status_hint = "设置已保存 · MCP 重启后生效" if result.restart_required else "设置已保存"
        else:
            self._status_hint = "就绪"
        self._refresh_status_bar()
        self._refresh_context_usage()
        self.call_later(self._refresh_command_ui)
        self.query_one("#composer-input", ComposerTextArea).focus()

    def _model_completion_choices(self) -> tuple[tuple[str, str], ...]:
        config = getattr(self._turn_runner, "model_config", None)
        if config is None:
            return ()
        return tuple((ref, model_display_name(config, ref)) for ref in iter_model_refs(config))

    def _handle_model_selected(self, model_ref: str | None) -> str | None:
        if model_ref is not None:
            try:
                self._turn_runner.switch_model(model_ref)
            except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                self.notify(str(exc), title="切换模型失败", severity="error", timeout=10)
            else:
                config = self._turn_runner.model_config
                label = model_display_name(config, model_ref)
                self._status_hint = "就绪"
                self.notify(f"已切换为 {label}", title="模型")
                self._refresh_status_bar()
                self._refresh_context_usage()
        if len(self.screen_stack) == 1:
            self.query_one("#composer-input", ComposerTextArea).focus()
        self.call_later(self._refresh_command_ui)
        return getattr(self._turn_runner, "model_ref", None)

    def _update_composer_height(self) -> None:
        composer_input = self.query_one("#composer-input", ComposerTextArea)
        composer = self.query_one("#composer", Horizontal)

        visible_lines = max(
            MIN_COMPOSER_LINES,
            min(MAX_COMPOSER_LINES, composer_input.wrapped_document.height),
        )
        composer_input.styles.height = str(visible_lines)
        composer.styles.height = str(visible_lines + COMPOSER_FRAME_HEIGHT)
        self._fit_chat_panels()


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
