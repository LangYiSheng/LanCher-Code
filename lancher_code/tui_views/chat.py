from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.worker import Worker, WorkerState
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
from lancher_code.sessions.paths import SessionPaths
from lancher_code.sessions.repository import SessionRepositoryError
from lancher_code.slash_commands import (
    SessionCompletionChoice,
    SlashCompletionCandidate,
    SlashCompletionContext,
    SlashCommandRegistry,
    create_default_slash_command_registry,
)
from lancher_code.tui_views.composer import (
    CommandHintBar,
    ComposerSubmitted,
    ComposerTextArea,
    WorkPhaseCycleRequested,
    SlashCompletionChosen,
    SlashCompletionMenu,
    SlashMenuAcceptRequested,
    SlashMenuDismissRequested,
    SlashMenuNavigateRequested,
    StopTurnRequested,
)
from lancher_code.tui_views.message import BannerWidget, MessageWidget
from lancher_code.tui_views.permission import InlinePermissionPanel
from lancher_code.tui_views.settings import SettingsResult, SettingsScreen
from lancher_code.tui_views.model_picker import ModelPickerScreen
from lancher_code.tui_views.command_actions import CommandConfirmationScreen, save_command_setting
from lancher_code.tui_views.tasks import TaskScreenActions, TasksScreen, task_label
from lancher_code.tui_views.theme import apply_theme, theme_palette
from lancher_code.tui_views.exit_flow import ExitFlow
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
    #exit-hint { width: 1fr; height: auto; display: none; color: $warning; padding: 0 1; }
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
    .message--user, .message--assistant { padding: 0; }
    .message-label { color: $primary; text-style: bold; }
    .message-timeline { height: auto; width: 1fr; }
    .timeline-text { height: auto; width: 1fr; margin: 0; }
    .trace-section { height: auto; width: 1fr; margin: 0; }
    .timeline-separator { margin-top: 1; }
    .trace-header { height: 1; width: 1fr; color: $text-muted; }
    .trace-header:focus { text-style: bold underline; }
    .trace-body { height: auto; width: 1fr; padding-left: 2; color: $text-muted; }
    .tool-calls { height: auto; width: 1fr; padding-left: 2; }
    .tool-calls.-single { padding-left: 0; }
    .tool-call-trace { margin: 0; }
    .tool-call-body { padding-bottom: 0; }
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
        Binding("ctrl+c", "request_quit", "取消/退出", priority=True),
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
        self._ui_closing = False
        self._exit_flow = ExitFlow()
        self._exit_hint_timer: Timer | None = None
        self._compaction_worker: Worker | None = None
        self._shutdown_process_count = 0
        self._status_refresh_timer: Timer | None = None
        self._chat_started = False
        self._message_widgets: dict[str, MessageWidget] = {}
        self._task_message_ids: set[str] = set()
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
                yield SlashCompletionMenu()
                yield Static("", id="exit-hint", markup=False)
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
        self._status_refresh_timer = self.set_interval(1.0, self._refresh_status_bar)
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
            if not self._ui_closing and self.is_running:
                composer = self.query_one("#composer-input", ComposerTextArea)
                composer.disabled = False
                self._status_hint = "就绪"
                self._refresh_composer_placeholder()
                self._refresh_status_bar()
                composer.focus()

    def _handle_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        if self._ui_closing:
            return
        self.query_one(BannerWidget).update_mcp_progress(progress)
        if progress.state == "complete":
            self._refresh_context_usage()

    def on_resize(self) -> None:
        self.screen.set_class(self.size.width < 64, "-narrow")
        self.screen.set_class(self.size.width < 48, "-tiny")
        self.call_after_refresh(self._refresh_status_bar)
        self.call_after_refresh(self._update_composer_height)
        self.call_after_refresh(self._fit_chat_panels)
        self.call_after_refresh(self._refresh_exit_hint)

    async def action_request_quit(self) -> None:
        decision = self._exit_flow.request_interrupt(
            busy=self._is_streaming or self._turn_runner.has_active_turn,
            stopping=self._turn_runner.is_stopping,
        )
        if decision == "cancel_work":
            if self._compaction_worker is not None:
                self._stop_current_work()
            elif self._is_streaming or self._turn_runner.has_active_turn:
                # 工作刚排入 worker 时也可能还没有 ActiveTurn，worker 会在入口检查停止标记。
                self._status_hint = "正在停止 · 草稿和队列会保留"
                self._turn_runner.cancel_active_turn()
                self._refresh_status_bar()
            return
        if decision == "wait_for_stop":
            self.notify("正在停止本轮，请稍候。", title="正在收尾")
            return
        if decision == "arm_exit":
            message = self._exit_confirmation_text()
            self.notify(message, title="退出确认", timeout=3)
            if self._exit_hint_timer is not None:
                self._exit_hint_timer.stop()
            self._exit_hint_timer = self.set_interval(0.2, self._refresh_exit_hint)
            self._refresh_exit_hint()
            return
        if decision == "exit":
            self._shutdown_process_count = self._turn_runner.application_process_count
            self._ui_closing = True
            self.exit(0)

    async def on_event(self, event: events.Event) -> None:
        # 在输入转交给控件之前解除确认，输入框和弹窗消费按键也不会留下旧确认。
        interaction_events = (
            events.Key, events.MouseDown, events.Paste,
            events.MouseScrollUp, events.MouseScrollDown,
            events.MouseScrollLeft, events.MouseScrollRight,
        )
        if isinstance(event, interaction_events) and not event.is_forwarded:
            if not isinstance(event, events.Key) or event.key != "ctrl+c":
                self._exit_flow.interact()
                self._refresh_exit_hint()
        await super().on_event(event)

    def _exit_confirmation_text(self) -> str:
        count = self._turn_runner.application_process_count
        suffix = f" · 退出将结束 {count} 个托管进程" if count else ""
        return "再按一次 Ctrl+C 退出（3 秒内）" + suffix

    def _refresh_exit_hint(self) -> None:
        if self._ui_closing or not self.is_running:
            return
        hint = next(iter(self.query("#exit-hint")), None)
        if hint is None:
            return
        hint.display = self._exit_flow.is_armed
        menu = self.query_one(SlashCompletionMenu)
        hint_bar = self.query_one(CommandHintBar)
        # 小终端确认退出时先让提示、草稿和 HUD 可见；候选身份和输入内容仍保留。
        hide_completion = hint.display and self.size.height < 24
        was_hidden = not menu.display
        menu.display = bool(self._slash_menu_matches) and not hide_completion
        hint_bar.display = bool(hint_bar.content) and not hide_completion
        if hint.display:
            hint.update(self._exit_confirmation_text())
        elif self._exit_hint_timer is not None:
            self._exit_hint_timer.stop()
            self._exit_hint_timer = None
        self._fit_chat_panels()
        if was_hidden and menu.display:
            self.call_after_refresh(menu.reveal_active)

    def _request_explicit_exit(self) -> None:
        if self._exit_flow.request_exit() == "exit":
            self._shutdown_process_count = self._turn_runner.application_process_count
            self._ui_closing = True
            self.exit(0)

    def _stop_current_work(self) -> None:
        if self._compaction_worker is not None:
            self._compaction_worker.cancel()
            self._status_hint = "正在停止上下文压缩"
            if not self._turn_runner.is_compacting:
                # 取消发生在 worker 第一条指令之前，没有协程 finally 可替界面收尾。
                self._compaction_worker = None
                self._is_streaming = False
                self._exit_flow.work_finished()
                self._status_hint = "压缩已停止"
                composer = self.query_one(ComposerTextArea)
                composer.disabled = False
                composer.focus()
                self.call_later(self._refresh_command_ui)
            self._refresh_status_bar()
        else:
            if self._turn_runner.cancel_active_turn():
                self._status_hint = "正在停止 · 草稿和队列会保留"
                self._refresh_status_bar()

    @on(StopTurnRequested)
    def handle_stop_turn_requested(self, event: StopTurnRequested) -> None:
        event.stop()
        if self._is_streaming or self._turn_runner.has_active_turn:
            if self._exit_flow.request_interrupt(busy=True) == "cancel_work":
                self._stop_current_work()

    @on(TextArea.Changed, "#composer-input")
    async def handle_composer_changed(self) -> None:
        self._exit_flow.interact()
        self._refresh_exit_hint()
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

    @on(SlashCompletionChosen)
    async def handle_slash_command_chosen(self, event: SlashCompletionChosen) -> None:
        await self._accept_completion(event.candidate_key)

    @on(WorkPhaseCycleRequested)
    async def handle_work_phase_cycle_requested(self) -> None:
        if self._is_streaming or self._ui_closing:
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
                match = self._slash_command_registry.parse_submission(text)
                if match is not None and self._command_allowed_while_busy(match.definition.name, match.arguments_text):
                    await self._execute_slash_command(match.definition.name, match.arguments_text)
                    if not self._command_preserve_input:
                        event.composer.clear()
                        await self._refresh_command_ui()
                    return
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
            self._close_hud_details()
            await self._refresh_command_ui()
            await self._refresh_pending_queue()
            return

        slash_match = self._slash_command_registry.parse_submission(text)
        if slash_match is not None:
            payload = await self._execute_slash_command(slash_match.definition.name, slash_match.arguments_text)
            if self._command_preserve_input:
                return
            event.composer.clear()
            await self._refresh_command_ui()
            if payload is None:
                return
            text = payload
        else:
            if text.startswith("/"):
                self.notify("未知命令；输入 / 查看命令列表。", severity="warning")
                return
            event.composer.clear()
            await self._refresh_command_ui()

        self._begin_turn(text)

    def _begin_turn(self, text: str, *, queued: bool = False) -> None:
        if self._is_streaming or self._ui_closing:
            return

        if not self._chat_started:
            self._chat_started = True
            self.query_one(BannerWidget).set_compact(True)
            self.query_one("#chat-view", VerticalScroll).set_class(True, "-banner-collapsed")

        self._is_streaming = True
        self._exit_flow.work_started()
        self._turn_succeeded = False
        self._task_message_ids.clear()
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
        user_message_accepted = queued
        submission_failed = False
        try:
            if self._exit_flow.state == "stopping":
                submission_failed = True
                self._turn_runner.pause_queue()
                return
            stream = self._turn_runner.run_next_queued_turn() if queued else self._turn_runner.run_user_turn(text)
            # 消费事件期间也可能关闭界面；显式关闭生成器，等待执行器清理。
            async with aclosing(stream):
                async for event in stream:
                    if event.kind == "user_message_created":
                        user_message_accepted = True
                    elif event.kind == "turn_failed":
                        submission_failed = True
                    await self._consume_turn_event(event)
        except asyncio.CancelledError:
            submission_failed = True
            raise
        except Exception as exc:
            submission_failed = True
            logger.exception(
                "event=tui_turn_worker_failed exception_type=%s", type(exc).__name__
            )
            self._turn_runner.pause_queue()
            self._status_hint = "未完成 · 队列已暂停"
            self.notify(str(exc), title="本轮未完成", severity="error")
        finally:
            self._is_streaming = False
            self._exit_flow.work_finished()
            if self.is_running and not self._ui_closing:
                if submission_failed and not user_message_accepted:
                    composer = self.query_one(ComposerTextArea)
                    # 仅恢复尚未进入 Session 的输入；保留用户随后写下的新草稿。
                    if not composer.text:
                        composer.text = text
                        composer.cursor_location = composer.document.end
                    if not self._session_controller.state.messages:
                        await self._restore_session_view()
                await self._finish_turn_view()
            else:
                self._pending_permissions.clear()
                try:
                    self._session_controller.flush()
                except (LanCherError, ValueError, OSError):
                    logger.exception("event=session_flush_failed")

    async def on_unmount(self) -> None:
        self._ui_closing = True
        if self._exit_hint_timer is not None:
            self._exit_hint_timer.stop()
            self._exit_hint_timer = None
        if self._status_refresh_timer is not None:
            self._status_refresh_timer.stop()
            self._status_refresh_timer = None
        # Textual 取消 worker 后不会等待所有后台执行器；退出前显式完成收尾。
        self._shutdown_process_count = max(self._shutdown_process_count, self._turn_runner.application_process_count)
        try:
            await self._turn_runner.shutdown()
        except Exception:
            # 应用 finally 仍会取得同一清理任务的失败结果，并在普通终端报告。
            logger.exception("event=tui_shutdown_failed")

    async def _finish_turn_view(self) -> None:
        for request_id in list(self._pending_permissions):
            await self._close_inline_permission(request_id)
        try:
            self._session_controller.flush()
        except (LanCherError, ValueError, OSError) as exc:
            self.notify(str(exc), title="会话持久化失败", severity="error", timeout=10)
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
        if self._ui_closing or self._is_streaming or self._turn_runner.has_active_turn or self._turn_runner.queue_paused:
            return
        items = self._turn_runner.pending_inputs
        if items and items[0].state == "pending" and items[0].delivery == "follow_up":
            self._begin_turn("", queued=True)

    def _refresh_status_bar(self) -> None:
        # Textual 先卸载控件，再等待 App 的异步收尾；已排队的刷新也须止步。
        if self._ui_closing or not self.is_running:
            return
        status_left = next(iter(self.query("#status-left")), None)
        if status_left is None or not status_left.is_mounted:
            return
        usage = self._session_controller.total_usage()
        center_text = self._status_hint or ("正在处理" if self._is_streaming else "就绪")
        execution = self._turn_runner.execution_summary()
        badges = []
        if execution["background"]:
            badges.append(f"后台 {execution['background']}")
        if execution["notifications"]:
            badges.append(f"通知 {execution['notifications']} /tasks")
        process_badges = " · ".join(badges)
        right_text = process_badges
        status_center = self.query_one("#status-center", Static)
        status_right = self.query_one("#status-right", Static)

        banner = self.query_one(BannerWidget)
        estimate = banner._context_usage_status.replace("上下文 ", "预计 ")
        action = getattr(self._ui_config, "busy_enter_action", "follow_up")
        enter_action = {"follow_up": "排队", "steer": "补充", "draft": "草稿"}[action] if self._is_streaming else "发送"
        composer = self.query_one(ComposerTextArea)
        is_command = composer.text.lstrip().startswith("/")
        if is_command:
            enter_action = "等待" if self._is_streaming and not self._command_text_allowed_while_busy(composer.text) else "填入" if composer.slash_menu_active and composer.slash_enter_accepts else "执行"
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
            if process_badges:
                model_limit = max(5, model_limit - cell_len(f" · {compact_state}"))
            model_text = Text(model)
            model_text.truncate(model_limit, overflow="ellipsis")
            model = model_text.plain
            if self.size.width < 48:
                first = f"{model} · {phase}" + (f" · {compact_state}" if process_badges else "")
                last = process_badges or f"{compact_state} · Enter {enter_action}"
                label = f"{first}\n{policy} · {estimate}\n{last}"
            else:
                first = f"{model} · {phase} · {policy}" + (f" · {compact_state}" if process_badges else "")
                last = process_badges or f"{compact_state} · Enter {enter_action}"
                label = f"{first}\n{estimate} · {last}"
        else:
            # 模型名按终端格宽截断，给阶段与权限保留位置。
            # 使用本轮状态的宽度，不能沿用状态变化前上一帧的布局。
            hud_width = max(1, min(self.size.width, 112) - 4)
            center_width = min(cell_len(f"{estimate} · {center_text}"), (hud_width * 35 + 99) // 100)
            available = hud_width - center_width - cell_len(right_text) - 1
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
        status_right.update(right_text)
        self.query_one(StageBar).update_phase(self._session_controller.work_phase, busy=self._is_streaming)
        config = getattr(self._turn_runner, "model_config", None)
        ref = getattr(self._turn_runner, "model_ref", None)
        notification_hint = " · /tasks 查看任务与输出" if execution["notifications"] else ""
        details = (
            f"本次模型：{self._status_left_text()}\n"
            f"工作目录：{self._session_controller._cwd}\n"
            f"会话：{self._session_controller.session_title or '新对话'} · {self._session_controller.session_id or '首条消息后创建'}\n"
            f"会话工作目录：{self._session_controller.paths.workspace if self._session_controller.paths else '尚未创建'}\n"
            f"模型引用：{ref or self._provider_config.model}\n"
            f"新对话默认：{getattr(config, 'default_model', None) or '当前配置'}\n"
            f"{self._format_usage_text(usage)} · {banner._context_usage_status}\n"
            f"托管进程：运行 {execution['running']} · 会话后台 {execution['background']} · 排队 {execution['waiting']}\n"
            f"未读完成通知：{execution['notifications']}{notification_hint}\n"
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
            ("本轮结束后执行 · 草稿已保留" if self._is_streaming and not self._command_text_allowed_while_busy(composer.text) else f"Enter {enter_action} · Tab 补全 · Esc 关闭") if is_command else
            busy_help + " · Ctrl+Enter 补充 · Esc 停本轮" if self._is_streaming else "Enter 发送 · Shift+Enter 换行"
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

    async def _sync_message_widget(self, message_id: str) -> None:
        widget = self._message_widgets[message_id]
        await widget.update_from_message(self._session_controller.get_message(message_id))

    async def _consume_turn_event(self, event: TurnEvent) -> None:
        if event.kind == "user_message_created":
            self._close_hud_details()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        follow_bottom = chat_view.is_vertical_scroll_end
        self._apply_turn_event(event)
        if event.message is not None and event.kind in {"user_message_created", "assistant_message_started"}:
            await self._mount_message_widget(event.message)
            if event.kind == "assistant_message_started":
                self._task_message_ids.add(event.message.id)
        elif event.message is not None:
            await self._sync_message_widget(event.message.id)
        if event.kind == "turn_completed":
            for message_id in self._task_message_ids:
                widget = self._message_widgets.get(message_id)
                if widget is not None:
                    widget.collapse_for_completion()
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
        if event.kind in {"phase_changed", "policy_changed"}:
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

    def _close_hud_details(self) -> None:
        self._details_open = False
        self.query_one("#status-details", Static).display = False

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
        session_completion = bool(self._slash_menu_matches) and (
            self._slash_menu_matches[self._slash_menu_index].presentation == "session"
        )
        self.query_one("#composer-region").styles.max_height = "90%" if short and session_completion else "75%"
        self.query_one(CommandHintBar).styles.max_height = (4 if session_completion else 1) if short else None
        self.query_one(SlashCompletionMenu).styles.max_height = (3 if session_completion else 4) if short else 7
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
        composer = self.query_one(ComposerTextArea)
        line_limit = 1 if self._pending_permissions else 3
        lines = max(1, min(line_limit, composer.wrapped_document.height))
        composer.styles.height = lines
        self.query_one("#composer").styles.height = lines + 1
        # 预留横幅、阶段、聊天、完整 HUD、输入边框与操作行。
        hud_extra = 1 if self.size.width < 48 else 0
        budget = max(3, self.size.height - 8 - lines - hud_extra)
        if session_completion and self.query_one(SlashCompletionMenu).display:
            # 完整 UUID 的提示优先保留；暂停队列可以缩到标题行，内容在自己的视口滚动。
            hint_width = max(1, min(self.size.width, 112) - 4)
            uuid_lines = (len(self._slash_menu_matches[self._slash_menu_index].value) + hint_width - 1) // hint_width
            budget -= 3 + min(4, 1 + uuid_lines)
        queue_height = min(3, max(1, budget)) if queue.display else 0
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
            config = getattr(self._turn_runner, "model_config", None)
            if config is not None and not self._is_streaming:
                self.push_screen(ModelPickerScreen(config, self._turn_runner.model_ref), self._handle_model_selected)
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
            self.call_later(widget._sync_view)
        self.query_one(BannerWidget).refresh()
        self._refresh_status_bar()

    def _apply_models_settings(self, config) -> str | None:
        self._turn_runner.reload_models(config)
        self._refresh_status_bar()
        self._refresh_context_usage()
        return self._turn_runner.model_ref

    async def _refresh_command_ui(self) -> None:
        composer = self.query_one("#composer-input", ComposerTextArea)
        menu = self.query_one(SlashCompletionMenu)
        hint_bar = self.query_one(CommandHintBar)
        composer.clear_accepted_slash_command_if_needed()
        if self._is_streaming and not self._command_text_allowed_while_busy(composer.text):
            self._slash_menu_matches = []
            self._slash_menu_index = 0
            composer.slash_menu_active = False
            await menu.set_candidates([], None)
            hint_bar.set_hint("本轮结束后可执行命令 · 草稿已保留" if composer.text.lstrip().startswith("/") else "")
            self._refresh_exit_hint()
            self._refresh_status_bar()
            return

        cursor_at_end = composer.cursor_location == composer.document.end
        processes = self._turn_runner.list_processes() if composer.text.lstrip().startswith("/tasks") else []
        sessions = []
        session_listing_error = None
        if cursor_at_end and composer.text.lstrip().startswith("/session"):
            try:
                sessions = self._session_controller.list_sessions()
            except (LanCherError, ValueError, OSError) as exc:
                session_listing_error = str(exc)
        matches = (
            self._slash_command_registry.complete(
                SlashCompletionContext(
                    text=composer.text,
                    session_choices=tuple(SessionCompletionChoice(
                        item.session_id, item.title, item.updated_at, item.archived,
                    ) for item in sessions),
                    active_session_id=self._session_controller.session_id,
                    process_choices=tuple((str(item["process_id"]), task_label(item)) for item in processes),
                    model_choices=self._model_completion_choices(),
                    active_model_ref=getattr(self._turn_runner, "model_ref", None),
                    default_model_ref=getattr(getattr(self._turn_runner, "model_config", None), "default_model", None),
                    permission_policy=self._session_controller.permission_policy,
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
        menu.styles.max_height = 4 if self.size.height < 24 else 7
        composer.slash_menu_active = bool(matches)
        self._fit_chat_panels()
        composer.slash_enter_accepts = not matches or not all(item.optional for item in matches)
        if matches and active_key is not None:
            active = matches[self._slash_menu_index]
            hint_bar.set_hint("会话列表不可用：" + session_listing_error if session_listing_error else self._completion_hint(active))
            self._refresh_exit_hint()
            self._refresh_status_bar()
            return

        self._slash_menu_matches = []
        self._slash_menu_index = 0
        composer.slash_menu_active = False

        hint_bar.set_hint("会话列表不可用：" + session_listing_error if session_listing_error else self._slash_command_registry.hint(composer.text))
        self._refresh_exit_hint()
        self._refresh_status_bar()

    def _completion_hint(self, candidate: SlashCompletionCandidate) -> str:
        if candidate.presentation == "session":
            title = Text(candidate.display)
            if self.size.height < 24:
                # 小屏优先保留完整 UUID。长标题可在 /session list 的滚动详情中完整阅读。
                title.truncate(max(1, min(self.size.width, 112) - 4), overflow="ellipsis")
            return title.plain + "\n" + candidate.value
        keys = "Enter 执行 · Tab 填入可选参数" if candidate.optional else "↑↓ 选择 · Tab/Enter 填入 · Esc 关闭"
        detail = candidate.detail or candidate.description
        if self.size.height < 24 and candidate.optional:
            return candidate.description
        if self.size.width < 64 and candidate.presentation != "session":
            detail = candidate.display + " · " + candidate.description
        return detail + " · " + keys

    async def _move_slash_menu(self, direction: int) -> None:
        if not self._slash_menu_matches:
            return
        self._slash_menu_index = (self._slash_menu_index + direction) % len(self._slash_menu_matches)
        menu = self.query_one(SlashCompletionMenu)
        await menu.set_candidates(
            self._slash_menu_matches,
            self._current_active_completion_key(),
        )
        self.query_one(CommandHintBar).set_hint(self._completion_hint(self._slash_menu_matches[self._slash_menu_index]))

    async def _accept_slash_menu_selection(self) -> None:
        candidate_key = self._current_active_completion_key()
        if candidate_key is None:
            composer = self.query_one(ComposerTextArea)
            if (self._is_streaming and not self._command_text_allowed_while_busy(composer.text)) or composer.cursor_location != composer.document.end:
                return
            advanced = self._slash_command_registry.advance_text(composer.text)
            if advanced is not None:
                composer.text = advanced
                composer.cursor_location = composer.document.end
                await self._refresh_command_ui()
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
        composer.remember_accepted_slash_command(composer.text)
        await self.query_one(SlashCompletionMenu).set_candidates([], None)
        self.query_one(CommandHintBar).set_hint("菜单已关闭 · 草稿已保留")
        self._refresh_status_bar()

    def _current_active_completion_key(self) -> str | None:
        if not self._slash_menu_matches:
            return None
        if self._slash_menu_index >= len(self._slash_menu_matches):
            self._slash_menu_index = 0
        return self._slash_menu_matches[self._slash_menu_index].key

    async def _execute_slash_command(self, command_name: str, arguments_text: str) -> str | None:
        self._command_preserve_input = False
        try:
            if (self._is_streaming or self._turn_runner.has_active_turn) and not self._command_allowed_while_busy(command_name, arguments_text):
                raise ValueError("当前任务结束后可执行命令；草稿已保留。")
            self._slash_command_registry.validate(command_name, arguments_text)
            if command_name in {"session", "model", "permissions", "settings"} and not arguments_text.strip():
                composer = self.query_one(ComposerTextArea)
                composer.text = f"/{command_name} "
                composer.cursor_location = composer.document.end
                composer.remember_accepted_slash_command("")
                self._command_preserve_input = True
                await self._refresh_command_ui()
                composer.focus()
                return None
            return await self._dispatch_slash_command(command_name, arguments_text)
        except (LanCherError, ValueError, RuntimeError, OSError) as exc:
            self._command_preserve_input = True
            self.notify(str(exc), title="命令未执行", severity="warning", timeout=10)
            return None

    @staticmethod
    def _command_allowed_while_busy(command_name: str, arguments_text: str) -> bool:
        return command_name == "tasks" or (command_name == "session" and arguments_text.strip() == "stop")

    def _command_text_allowed_while_busy(self, text: str) -> bool:
        match = self._slash_command_registry.parse_submission(text)
        return match is not None and self._command_allowed_while_busy(match.definition.name, match.arguments_text)

    async def _dispatch_slash_command(self, command_name: str, arguments_text: str) -> str | None:
        if command_name == "exit":
            self._request_explicit_exit()
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
            self._apply_turn_event(self._turn_runner.set_permission_policy(arguments_text.strip()))
            self._refresh_status_bar()
            self.notify("本次审批策略已更新；工作阶段保持不变。", title="审批策略")
            return None

        if command_name == "model":
            config = getattr(self._turn_runner, "model_config", None)
            if config is None:
                raise ValueError("模型目录尚未加载。")
            ref = arguments_text.strip()
            self._turn_runner.switch_model(ref)
            self._refresh_status_bar()
            self._refresh_context_usage()
            self.notify(f"已切换为 {model_display_name(config, ref)}", title="本次模型")
            return None

        if command_name == "settings":
            if self._settings_service is None:
                raise ValueError("设置服务尚未加载。")
            args = arguments_text.split()
            if args[0] != "open":
                save_command_setting(self, args[0], args[1])
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
            self._exit_flow.work_started()
            self._status_hint = "正在压缩上下文..."
            composer = self.query_one("#composer-input", ComposerTextArea)
            composer.disabled = True
            self._refresh_status_bar()
            self.notify("正在压缩上下文...", title="上下文压缩")
            self._compaction_worker = self._run_manual_compaction()
            return None

        if command_name == "session":
            await self._execute_session_command(arguments_text)
            return None

        if command_name == "tasks":
            await self._execute_tasks_command(arguments_text)
            return None

        return None

    @work(group="compaction", exclusive=True, exit_on_error=False)
    async def _run_manual_compaction(self) -> None:
        # 压缩交给 worker，主界面的消息循环才能继续处理 Ctrl+C。
        status = "就绪"
        try:
            result = await self._turn_runner.compact_context()
        except asyncio.CancelledError:
            status = "压缩已停止"
            raise
        except Exception as exc:
            status = "压缩未完成"
            self._command_preserve_input = True
            if not self._ui_closing and self.is_running:
                self.notify(str(exc), title="上下文压缩失败", severity="error", timeout=10)
                composer = self.query_one(ComposerTextArea)
                if not composer.text:
                    composer.text = "/compact"
                    composer.cursor_location = composer.document.end
        else:
            if not self._ui_closing and self.is_running:
                self.notify(f"已压缩，token 从 {result.before_tokens} 降至 {result.after_tokens}",
                            title="上下文压缩", timeout=10)
        finally:
            self._compaction_worker = None
            self._is_streaming = False
            self._exit_flow.work_finished()
            self._status_hint = status
            if not self._ui_closing and self.is_running:
                composer = self.query_one(ComposerTextArea)
                composer.disabled = False
                composer.focus()
                self._refresh_status_bar()
                self._refresh_context_usage()
                await self._refresh_command_ui()

    @on(Worker.StateChanged)
    async def handle_compaction_worker_state(self, event: Worker.StateChanged) -> None:
        if event.worker is not self._compaction_worker:
            return
        if event.state not in {WorkerState.CANCELLED, WorkerState.ERROR, WorkerState.SUCCESS}:
            return
        if not self._is_streaming:
            # eager task 可能在 worker 引用赋值前完成；已有 finally 的结果不能被覆盖。
            self._compaction_worker = None
            return
        # worker 尚未进入协程就取消时，协程内的 finally 不会执行。
        self._compaction_worker = None
        self._is_streaming = False
        self._exit_flow.work_finished()
        self._status_hint = "压缩已停止" if event.state == WorkerState.CANCELLED else "压缩未完成"
        if not self._ui_closing and self.is_running:
            composer = self.query_one(ComposerTextArea)
            composer.disabled = False
            composer.focus()
            self._refresh_status_bar()
            await self._refresh_command_ui()

    async def _execute_session_command(self, arguments_text: str, *, confirmed: bool = False) -> None:
        arguments = arguments_text.split(maxsplit=2)
        action = arguments[0]
        if action == "stop":
            await self._turn_runner.stop_session()
            self._status_hint = "当前会话已停止 · 草稿、队列和日志保留"
            self._refresh_status_bar()
            await self._refresh_pending_queue()
            self.notify("当前会话的本轮及全部托管进程已收尾。", title="Session")
            return
        if self._is_streaming or self._turn_runner.has_active_turn:
            raise ValueError("本轮结束后才能管理会话，命令草稿已保留。")
        if action in {"archive", "remove"} and arguments[1] == self._session_controller.session_id:
            raise SessionRepositoryError("当前会话不能归档或删除，请先运行 /session new。")
        if not confirmed and action in {"archive", "remove"}:
            paths = SessionPaths.for_session(self._session_controller._cwd, arguments[1])
            if not paths.events.is_file():
                raise SessionRepositoryError("目标会话不存在，请输入完整 UUID。")
            target_title = "无法读取标题"
            try:
                target = next((item for item in self._session_controller.list_sessions() if item.session_id == arguments[1]), None)
                if target is not None:
                    target_title = target.title
            except (LanCherError, ValueError, OSError):
                if action != "remove":
                    raise
            description = (
                f"{'归档' if action == 'archive' else '删除'}项目会话：{target_title}\nUUID：{arguments[1]}"
                + ("\n删除包含对话记录、计划和会话工作文件，无法撤销。" if action == "remove" else "\n归档后记录和工作文件保留。")
            )
            session_id = self._session_controller.session_id
            original_state = self._session_controller.state
            composer = self.query_one(ComposerTextArea)
            original_text = composer.text
            consumed = False

            async def resolve(accepted: bool) -> None:
                nonlocal consumed
                if consumed:
                    return
                consumed = True
                if not accepted:
                    composer.focus()
                    return
                try:
                    if self._is_streaming or self._turn_runner.has_active_turn or session_id != self._session_controller.session_id or original_state is not self._session_controller.state:
                        raise ValueError("当前会话状态已改变，请重新提交命令。")
                    await self._execute_session_command(arguments_text, confirmed=True)
                except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                    self.notify(str(exc), title="命令未执行", severity="warning")
                else:
                    if composer.text == original_text:
                        composer.clear()
                await self._refresh_command_ui()
                composer.focus()

            self._command_preserve_input = True
            self.push_screen(CommandConfirmationScreen(description, "/session " + arguments_text), resolve)
            return
        if action == "list" and len(arguments) == 1:
            sessions = self._session_controller.list_sessions()
            if not sessions:
                message = "当前项目还没有会话；发送首条消息时自动创建。"
            else:
                active = self._session_controller.session_id
                message = "\n".join(
                    f"{'* ' if item.session_id == active else '  '}{item.title} · {item.session_id[:8]}"
                    f"{' · 已归档' if item.archived else ''} · "
                    f"{item.updated_at.astimezone().strftime('%Y-%m-%d %H:%M')} · "
                    f"{item.message_count} 条消息 · {item.permission_rule_count} 条会话权限"
                    for item in sessions
                )
            self.push_screen(ReadOnlyDetailsScreen("项目会话\n" + message))
            return

        if action == "new" and len(arguments) == 1:
            self._turn_runner.new_session()
            await self._restore_session_view()
            self.notify("已打开新对话；发送首条消息时创建会话。", title="Session")
            return

        if action == "archive" and len(arguments) == 2:
            self._session_controller.archive_session(arguments[1])
            self.notify(f"已归档会话：{arguments[1]}", title="Session")
            return

        if action == "remove" and len(arguments) == 2:
            self._session_controller.remove_session(arguments[1])
            self.notify(f"已删除会话：{arguments[1]}", title="Session")
            return

        if action == "rename" and len(arguments) == 3:
            self._session_controller.rename_session(arguments[1], arguments[2])
            self._refresh_status_bar()
            self.notify(f"会话标题已改为：{arguments[2]}", title="Session")
            return

        if action == "resume" and len(arguments) == 2:
            permission_count = self._turn_runner.resume_session(arguments[1])
            await self._restore_session_view()
            notice = getattr(self._turn_runner, "model_notice", "")
            self.notify(
                f"已恢复会话：{arguments[1]}（恢复 {permission_count} 条会话权限）" + (f"\n{notice}" if notice else ""),
                title="Session",
            )
            return

        raise SessionRepositoryError("参数不正确，请查看 /session 的命令提示。")

    async def _execute_tasks_command(self, arguments_text: str) -> None:
        arguments = arguments_text.split()
        action = arguments[0] if arguments else "show"
        process_id = arguments[1] if len(arguments) > 1 else None
        session_id = self._session_controller.session_id
        if action == "stop" and process_id:
            result = await self._turn_runner.stop_process(process_id, session_id=session_id)
            storage_error = getattr(result, "storage_error", None)
            self.notify("进程已停止，但日志保存失败，请查看任务详情。" if storage_error else "进程及其托管子进程已停止，日志保留。", title="进程任务", severity="warning" if storage_error else "information")
            return
        if action == "background" and process_id:
            await self._turn_runner.background_process(process_id, session_id=session_id)
            self.notify("已转交会话后台；停止本轮和切换对话后继续运行。", title="进程任务")
            return
        if process_id and process_id not in {str(item["process_id"]) for item in self._turn_runner.list_processes(session_id=session_id)}:
            raise ValueError("当前会话没有此进程，请输入完整进程 UUID。")
        runner = self._turn_runner
        async def read_output(target: str, cursor: int) -> dict[str, object]:
            return runner.read_process_output(target, cursor=cursor, max_chars=16000, session_id=session_id)
        # 所有回调绑定打开窗口时的 Session，不能跟随界面切换改归属。
        callbacks = TaskScreenActions(
            list_tasks=lambda: runner.list_processes(session_id=session_id),
            read_output=read_output,
            stop=lambda target: runner.stop_process(target, session_id=session_id),
            background=lambda target: runner.background_process(target, session_id=session_id),
            write_input=lambda target, text: runner.write_process_input(target, text, session_id=session_id),
            stop_session=lambda: runner.stop_session(session_id=session_id),
        )
        self.push_screen(TasksScreen(session_id, callbacks, selected_process_id=process_id))

    async def _restore_session_view(self) -> None:
        chat_view = self.query_one("#chat-view", VerticalScroll)
        for child in list(chat_view.children):
            await child.remove()
        self._message_widgets.clear()
        self._task_message_ids.clear()
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

    @property
    def stopped_process_count(self) -> int:
        return self._app._shutdown_process_count

    def configure_mcp(self, manager: MCPClientManager, registry: ToolRegistry) -> None:
        self._app._mcp_manager = manager
        self._app._tool_registry = registry
        self._app.mcp_initialization_complete = not manager.has_servers

    def configure_settings(self, service: SettingsService) -> None:
        self._app._settings_service = service
