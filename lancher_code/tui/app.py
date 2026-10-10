from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

from lancher_code.tui.chat.styles import CHAT_CSS
from lancher_code.tui.chat.layout import ChatLayoutState, fit_chat_panels
from lancher_code.tui.chat.transcript import TranscriptView
from lancher_code.tui.chat.hud import HudPresenter, HudWidgets, HudState
from lancher_code.tui.chat.session_commands import SessionCommands
from lancher_code.tui.chat.task_commands import TaskCommands
from lancher_code.tui.chat.completion import CompletionController, CompletionWidgets
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.worker import Worker, WorkerState
from textual.widgets import Button, Static, TextArea

from lancher_code.errors import LanCherError
from lancher_code.providers.catalog import model_display_name
from lancher_code.permissions.models import PermissionRequest
from lancher_code.providers.models import ProviderConfig
from lancher_code.agent.events import TurnEvent
from lancher_code.config.models import UIConfig
from lancher_code.mcp.manager import MCPInitializationProgress
from lancher_code.config.settings import SettingsService
from lancher_code.logging_system import get_logger
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.tui.commands import SlashCommandRegistry, create_default_slash_command_registry
from lancher_code.tui.composer import (
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
from lancher_code.tui.message import BannerWidget
from lancher_code.tui.permission import InlinePermissionPanel
from lancher_code.tui.settings.screen import SettingsResult, SettingsScreen
from lancher_code.tui.model_picker import ModelPickerScreen
from lancher_code.tui.command_actions import save_command_setting
from lancher_code.tui.capabilities import CapabilityCommands
from lancher_code.tui.theme import apply_theme
from lancher_code.tui.exit_flow import ExitFlow
from lancher_code.tui.chat_controls import (
    ChatAction, StageBar, PendingQueue, PendingInputEditor, PlanPanel,
    PlanReviewScreen, PermissionPolicyScreen, ReadOnlyDetailsScreen,
)
from lancher_code.agent.runner import TurnRunner

logger = get_logger("tui.chat")

MIN_COMPOSER_LINES = 1
MAX_COMPOSER_LINES = 6
COMPOSER_FRAME_HEIGHT = 1
DEFAULT_PLACEHOLDER = "发送一条消息"
PLAN_PLACEHOLDER = "补充或修改计划，确认后再开始执行"

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
    CSS = CHAT_CSS

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
        settings_service: SettingsService | None = None,
    ) -> None:
        super().__init__()
        self._turn_runner = turn_runner
        self._provider_config = provider_config
        self._session_controller = session_controller
        self._ui_config = ui_config
        apply_theme(self, ui_config.theme)
        self._slash_command_registry = slash_command_registry or create_default_slash_command_registry()
        self._is_streaming = False
        self._ui_closing = False
        self._exit_flow = ExitFlow()
        self._exit_hint_timer: Timer | None = None
        self._compaction_worker: Worker | None = None
        self._manual_compaction_id: str | None = None
        self._shutdown_process_count = 0
        self._status_refresh_timer: Timer | None = None
        self._chat_started = False
        self._transcript = TranscriptView(session_controller, lambda: self._ui_config.show_thinking_status)
        self._status_hint = "就绪"
        self._context_estimate_label = "未校准估算"
        self._completion = CompletionController(
            self._slash_command_registry, session_controller, turn_runner,
            widgets=self._completion_widgets, size=lambda: self.size,
            busy=lambda: self._is_streaming, fit_panels=self._fit_chat_panels,
            refresh_chrome=self._refresh_completion_chrome,
        )
        self._pending_permissions: dict[str, PermissionRequest] = {}
        self._permission_ui_lock = asyncio.Lock()
        self._turn_succeeded = False
        self._details_open = False
        self._settings_service = settings_service
        self.mcp_initialization_complete = True
        self._session_commands = SessionCommands(
            session_controller, turn_runner, composer=lambda: self.query_one(ComposerTextArea),
            busy=lambda: self._is_streaming, notify=self.notify, open_screen=self.push_screen,
            restore_view=self._restore_session_view, refresh_completion=self._completion.refresh,
            refresh_queue=self._refresh_pending_queue, set_status=self._set_status_hint,
            preserve_input=self._preserve_command_input,
        )
        self._task_commands = TaskCommands(session_controller, turn_runner, self.notify, self.push_screen)
        self._capability_commands = CapabilityCommands(turn_runner, self.notify, self.push_screen)
        self._hud = HudPresenter(session_controller, turn_runner, provider_config.model,
                                 self._completion.text_allowed_while_busy)

    def _set_status_hint(self, text: str) -> None:
        self._status_hint = text
        self._refresh_status_bar()

    def _preserve_command_input(self) -> None:
        self._command_preserve_input = True

    def _completion_widgets(self) -> CompletionWidgets:
        return CompletionWidgets(self.query_one(ComposerTextArea), self.query_one(SlashCompletionMenu),
                                 self.query_one(CommandHintBar))

    def _refresh_completion_chrome(self) -> None:
        self._refresh_exit_hint()
        self._refresh_status_bar()

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield BannerWidget(Path.cwd())
            yield StageBar()
            yield self._transcript
            with Vertical(id="composer-region"):
                yield PlanPanel()
                yield PendingQueue()
                yield Vertical(id="approval-region")
                yield SlashCompletionMenu()
                yield Static("", id="exit-hint", markup=False)
                with Horizontal(id="composer"):
                    yield Static(">", id="prompt-glyph")
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
            with VerticalScroll(id="status-details-scroll"):
                yield Static(id="status-details", markup=False)

    async def on_mount(self) -> None:
        self.screen.set_class(self.size.width < 64, "-narrow")
        composer = self.query_one(ComposerTextArea)
        composer.disabled = False
        composer.focus()
        self._update_composer_height()
        self._refresh_phase_chrome()
        self._refresh_composer_placeholder()
        await self._completion.refresh()
        self._refresh_status_bar()
        self._refresh_context_usage()
        await self._refresh_pending_queue()
        self._refresh_plan_panel()
        self._status_refresh_timer = self.set_interval(1.0, self._refresh_status_bar)
        capabilities = self._turn_runner.capabilities
        capabilities.add_mcp_progress_callback(self._handle_mcp_progress)
        self._turn_runner.start_capabilities()
        if capabilities.mcp_progress is not None:
            self._handle_mcp_progress(capabilities.mcp_progress)

    def _handle_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        if self._ui_closing or not self.is_mounted:
            return
        self.query_one(BannerWidget).update_mcp_progress(progress)
        self.mcp_initialization_complete = progress.completed_servers >= progress.total_servers
        if progress.state in {"complete", "catalog_updated", "reconnected"}:
            self._refresh_context_usage()
            self.call_later(self._completion.refresh)

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
            if self._manual_compaction_id is not None or self._compaction_worker is not None:
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
        menu.display = bool(self._completion.matches) and not hide_completion
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
        if self._manual_compaction_id is not None or self._compaction_worker is not None:
            if self._compaction_worker is not None:
                self._compaction_worker.cancel()
            self._status_hint = "正在停止上下文压缩"
            if not self._turn_runner.is_compacting:
                # 取消发生在 worker 第一条指令之前，没有协程 finally 可替界面收尾。
                self._finish_manual_activity_if_running("cancelled")
                self._compaction_worker = None
                self._manual_compaction_id = None
                self._is_streaming = False
                self._exit_flow.work_finished()
                self._status_hint = "压缩已停止"
                composer = self.query_one(ComposerTextArea)
                composer.disabled = False
                composer.focus()
                self.call_later(self._completion.refresh)
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
        await self._completion.refresh()

    @on(SlashMenuNavigateRequested)
    async def handle_slash_menu_navigation(self, event: SlashMenuNavigateRequested) -> None:
        await self._completion.move(event.direction)

    @on(SlashMenuAcceptRequested)
    async def handle_slash_menu_accept(self) -> None:
        await self._completion.accept_selection()

    @on(SlashMenuDismissRequested)
    async def handle_slash_menu_dismiss(self) -> None:
        await self._completion.dismiss()

    @on(SlashCompletionChosen)
    async def handle_slash_command_chosen(self, event: SlashCompletionChosen) -> None:
        await self._completion.accept(event.candidate_key)

    @on(WorkPhaseCycleRequested)
    async def handle_work_phase_cycle_requested(self) -> None:
        if self._is_streaming or self._ui_closing:
            return
        phases = ("discuss", "plan", "execute")
        phase = self._session_controller.work_phase
        self._apply_turn_event(self._turn_runner.set_phase(phases[(phases.index(phase) + 1) % len(phases)]))
        await self._completion.refresh()
        self._refresh_status_bar()
        self.query_one("#composer-input", ComposerTextArea).focus()

    @on(ComposerSubmitted)
    async def handle_input_submitted(self, event: ComposerSubmitted) -> None:
        text = event.value.strip()
        if not text:
            return

        if self._is_streaming:
            if text.startswith("/"):
                match = self._slash_command_registry.parse_submission(text)
                if match is not None and self._completion.allowed_while_busy(match.definition.name, match.arguments_text):
                    await self._execute_slash_command(match.definition.name, match.arguments_text)
                    if not self._command_preserve_input:
                        event.composer.clear()
                        await self._completion.refresh()
                    return
                self._status_hint = "当前任务结束后可使用命令 · 输入已保留"
                self._refresh_status_bar()
                return
            delivery = event.delivery or self._ui_config.busy_enter_action
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
            await self._completion.refresh()
            await self._refresh_pending_queue()
            return

        slash_match = self._slash_command_registry.parse_submission(text)
        if slash_match is not None:
            payload = await self._execute_slash_command(slash_match.definition.name, slash_match.arguments_text)
            if self._command_preserve_input:
                return
            event.composer.clear()
            await self._completion.refresh()
            if payload is None:
                return
            text = payload
        else:
            if text.startswith("/"):
                self.notify("未知命令；输入 / 查看命令列表。", severity="warning")
                return
            event.composer.clear()
            await self._completion.refresh()

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
        self._transcript.turn_message_ids.clear()
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
        self._turn_runner.capabilities.remove_mcp_progress_callback(self._handle_mcp_progress)
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
        self._refresh_phase_chrome()
        self._refresh_composer_placeholder()
        await self._completion.refresh()
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
        # 卸载后的排队刷新不再读取控件。
        if self._ui_closing or not self.is_running:
            return
        status_left = next(iter(self.query("#status-left")), None)
        if status_left is None or not status_left.is_mounted:
            return
        widgets = HudWidgets(
            left=status_left, center=self.query_one("#status-center", Static),
            right=self.query_one("#status-right", Static), banner=self.query_one(BannerWidget),
            composer=self.query_one(ComposerTextArea), stage=self.query_one(StageBar),
            details=self.query_one("#status-details", Static),
            model=self.query_one("#chat-model", Button), policy=self.query_one("#chat-policy", Button),
            steer=self.query_one("#chat-steer", Button), queue=self.query_one("#chat-queue", Button),
            actions=self.query_one("#composer-actions"), help=self.query_one("#composer-help", Static),
        )
        self._hud.refresh(widgets, HudState(self.size.width, self.theme, self._status_hint,
            self._is_streaming, bool(self._pending_permissions), self._ui_config.busy_enter_action,
            self._context_estimate_label))

    def _refresh_context_usage(self) -> None:
        banner = self.query_one(BannerWidget)
        try:
            estimate = self._turn_runner.capabilities.context_usage()
            used_tokens = estimate["tokens"]
            self._context_estimate_label = "已校准估算" if estimate["source"] == "usage_calibrated" else "未校准估算"
        except Exception:
            logger.exception("event=tui_context_usage_estimate_failed")
            self._context_estimate_label = "估算暂不可用"
            banner.update_context_usage(None, self._session_controller.context_window)
            self._refresh_status_bar()
            return
        banner.update_context_usage(used_tokens, self._session_controller.context_window)
        self._refresh_status_bar()


    def _finish_manual_activity_if_running(self, status: str, error_text: str | None = None) -> bool:
        """worker 尚未开始也能收尾；与 Runner 的正常终结共用同一活动 ID。"""
        activity = self._session_controller.state.compaction_activities.get(self._manual_compaction_id)
        if activity is None:
            return False
        save_failed = False
        try:
            if activity.status == "running":
                activity = self._session_controller.finish_compaction(activity.id, status=status, error_text=error_text)
            else:
                activity = self._session_controller.get_compaction(activity.id)
        except (SessionRepositoryError, OSError) as exc:
            # 内存已终结、日志写入失败时，仍要停图标和解锁输入区。
            activity = self._session_controller.get_compaction(activity.id)
            save_failed = True
            logger.exception("event=compaction_status_save_failed activity_id=%s", activity.id)
            if not self._ui_closing and self.is_running:
                self.notify(str(exc), title="压缩状态保存失败", severity="error", timeout=10)
        widget = self._transcript.compaction_widgets.get(activity.id)
        if widget is not None:
            widget.update_activity(activity)
        return save_failed

    async def _consume_turn_event(self, event: TurnEvent) -> None:
        if event.kind == "user_message_created":
            self._close_hud_details()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        follow_bottom = chat_view.is_vertical_scroll_end
        self._apply_turn_event(event)
        await self._transcript.consume(event)
        if event.kind == "compaction_updated" and event.compaction is not None:
            if event.compaction.status == "completed" and event.compaction.trigger != "manual":
                self._refresh_context_usage()
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
            self._refresh_phase_chrome()
            self._refresh_composer_placeholder()
            return
        if event.kind == "progress_updated":
            self._status_hint = event.progress_message or ("正在处理" if self._is_streaming else "就绪")
            return
        if event.kind == "compaction_updated" and event.compaction is not None:
            activity = event.compaction
            self._status_hint = "正在压缩上下文" if activity.status == "running" else "正在处理" if self._turn_runner.has_active_turn else "就绪"
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

    def _refresh_phase_chrome(self) -> None:
        phase = self._session_controller.work_phase
        policy = self._session_controller.permission_policy
        glyph = "#" if phase == "plan" else {"default": ">", "acceptEdits": "+", "bypass": "!"}[policy]
        self.query_one("#prompt-glyph", Static).update(glyph)

    def _refresh_composer_placeholder(self) -> None:
        composer = self.query_one("#composer-input", ComposerTextArea)
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
        viewport = self.query_one("#status-details-scroll", VerticalScroll)
        viewport.display = self._details_open
        if self._details_open:
            viewport.focus()
        else:
            self.query_one(ComposerTextArea).focus()

    def _close_hud_details(self) -> None:
        self._details_open = False
        self.query_one("#status-details-scroll", VerticalScroll).display = False

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
        if not self.is_mounted:
            return
        if self.size.height < 24:
            self._details_open = False
        active = self._completion.matches[self._completion.index] if self._completion.matches else None
        fit_chat_panels(self.query_one("#root", Vertical), ChatLayoutState(
            self.size.width, self.size.height, self._chat_started, bool(self._pending_permissions), active))

    @on(Button.Pressed, "#composer-actions Button")
    async def handle_composer_button(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "chat-model":
            config = self._turn_runner.model_config
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
                    await self._completion.refresh()
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
        self._refresh_phase_chrome()
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
        apply_theme(self, ui_config.theme)
        for widget in self._transcript.message_widgets.values():
            widget.update_display_preferences(show_thinking=ui_config.show_thinking_status)
        self.query_one(BannerWidget).refresh()
        self._refresh_status_bar()

    def _apply_models_settings(self, config) -> str | None:
        self._turn_runner.reload_models(config)
        self._refresh_status_bar()
        self._refresh_context_usage()
        return self._turn_runner.model_ref

    async def _apply_mcp_settings(self) -> str:
        notice = await self._turn_runner.capabilities.reload_mcp()
        self._refresh_context_usage()
        await self._completion.refresh()
        return notice


    async def _execute_slash_command(self, command_name: str, arguments_text: str) -> str | None:
        self._command_preserve_input = False
        try:
            if (self._is_streaming or self._turn_runner.has_active_turn) and not self._completion.allowed_while_busy(command_name, arguments_text):
                raise ValueError("当前任务结束后可执行命令；草稿已保留。")
            self._slash_command_registry.validate(command_name, arguments_text)
            if command_name in {"session", "model", "permissions", "settings"} and not arguments_text.strip():
                composer = self.query_one(ComposerTextArea)
                composer.text = f"/{command_name} "
                composer.cursor_location = composer.document.end
                composer.remember_accepted_slash_command("")
                self._command_preserve_input = True
                await self._completion.refresh()
                composer.focus()
                return None
            return await self._dispatch_slash_command(command_name, arguments_text)
        except (LanCherError, ValueError, RuntimeError, OSError) as exc:
            self._command_preserve_input = True
            self.notify(str(exc), title="命令未执行", severity="warning", timeout=10)
            return None


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
            config = self._turn_runner.model_config
            if config is None:
                raise ValueError("模型目录尚未加载。")
            ref = arguments_text.strip()
            self._turn_runner.switch_model(ref)
            self._refresh_status_bar()
            self._refresh_context_usage()
            self.notify(f"已切换为 {model_display_name(config.providers, ref)}", title="本次模型")
            return None

        if command_name == "settings":
            if self._settings_service is None:
                raise ValueError("设置服务尚未加载。")
            args = arguments_text.split()
            if args[0] != "open":
                save_command_setting(self._settings_service, args[0], args[1],
                    apply_models=self._apply_models_settings, apply_ui=self._apply_ui_settings, notify=self.notify)
                return None
            self.push_screen(SettingsScreen(
                self._settings_service,
                current_model_ref=self._turn_runner.model_ref,
                on_models_saved=self._apply_models_settings,
                on_model_selected=self._handle_model_selected,
                on_ui_saved=self._apply_ui_settings,
                on_mcp_saved=self._apply_mcp_settings,
            ), self._handle_settings_result)
            return None

        if command_name == "compact":
            if arguments_text.strip():
                self.notify("用法：/compact", title="上下文压缩", severity="warning")
                return None
            chat_view = self.query_one("#chat-view", VerticalScroll)
            follow_bottom = chat_view.is_vertical_scroll_end
            previous_ids = set(self._session_controller.state.compaction_activities)
            try:
                activity = self._session_controller.begin_compaction("manual")
            except (SessionRepositoryError, OSError):
                # 开始记录写盘失败也有可展开的失败行；不要再次尝试保存。
                for activity_id in self._session_controller.state.compaction_activities.keys() - previous_ids:
                    await self._transcript.mount_compaction(self._session_controller.get_compaction(activity_id))
                if follow_bottom:
                    self.call_after_refresh(chat_view.scroll_end, animate=False)
                raise
            self._manual_compaction_id = activity.id
            self._is_streaming = True
            self._exit_flow.work_started()
            self._status_hint = "正在压缩上下文..."
            composer = self.query_one("#composer-input", ComposerTextArea)
            composer.disabled = True
            self._refresh_status_bar()
            try:
                await self._transcript.mount_compaction(activity)
            except BaseException as exc:
                self._finish_manual_activity_if_running("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", str(exc))
                self._stop_current_work()
                raise
            # 挂载控件也会让出事件循环；准备期间的停止不能再启动 worker。
            if self._manual_compaction_id != activity.id:
                self._transcript.compaction_widgets[activity.id].update_activity(self._session_controller.get_compaction(activity.id))
                return None
            if follow_bottom:
                self.call_after_refresh(chat_view.scroll_end, animate=False)
            self._compaction_worker = self._run_manual_compaction(activity.id)
            return None

        if command_name == "session":
            await self._session_commands.execute(arguments_text)
            return None

        if command_name == "tasks":
            await self._task_commands.execute(arguments_text)
            return None

        if command_name in {"skills", "mcp"}:
            action = arguments_text.split(maxsplit=1)[0] if arguments_text.strip() else "list"
            if action in {"list", "show"}:
                await self._capability_commands.execute(command_name, arguments_text)
                self._refresh_context_usage()
                await self._completion.refresh()
            else:
                composer = self.query_one(ComposerTextArea)
                current = self._slash_command_registry.parse_submission(composer.text.strip())
                draft = composer.text if (current is not None and current.definition.name == command_name
                                         and current.arguments_text == arguments_text) else None
                # 等待核心操作时保留原命令，输入和取消仍由消息循环处理。
                self._command_preserve_input = True
                self._run_capability_command(command_name, arguments_text, draft)
            return None

        return None

    @work(group="capability-commands", exclusive=False, exit_on_error=False)
    async def _run_capability_command(self, command_name: str, arguments_text: str, draft: str | None) -> None:
        try:
            await self._capability_commands.execute(command_name, arguments_text)
        except (LanCherError, ValueError, RuntimeError, OSError) as exc:
            if not self._ui_closing and self.is_running:
                self.notify(str(exc), title="命令未执行", severity="warning", timeout=10)
        else:
            if not self._ui_closing and self.is_running and draft is not None:
                composer = self.query_one(ComposerTextArea)
                # 操作期间用户编辑的新草稿不能被完成回调清掉。
                if composer.text == draft:
                    composer.clear()
        finally:
            if not self._ui_closing and self.is_running:
                self._refresh_context_usage()
                await self._completion.refresh()

    @work(group="compaction", exclusive=True, exit_on_error=False)
    async def _run_manual_compaction(self, activity_id: str) -> None:
        # 压缩交给 worker，主界面的消息循环才能继续处理 Ctrl+C。
        status = "就绪"
        try:
            await self._turn_runner.compact_context(activity_id=activity_id, on_activity=self._consume_turn_event)
        except asyncio.CancelledError:
            status = "压缩已停止"
            self._finish_manual_activity_if_running("cancelled")
            raise
        except Exception as exc:
            status = "压缩未完成"
            self._command_preserve_input = True
            save_failed = self._finish_manual_activity_if_running("failed", str(exc))
            if not self._ui_closing and self.is_running:
                if isinstance(exc, SessionRepositoryError) and not save_failed:
                    self.notify(str(exc), title="压缩状态保存失败", severity="error", timeout=10)
                composer = self.query_one(ComposerTextArea)
                if not composer.text:
                    composer.text = "/compact"
                    composer.cursor_location = composer.document.end
        finally:
            self._compaction_worker = None
            self._manual_compaction_id = None
            self._is_streaming = False
            self._exit_flow.work_finished()
            self._status_hint = status
            if not self._ui_closing and self.is_running:
                composer = self.query_one(ComposerTextArea)
                composer.disabled = False
                composer.focus()
                self._refresh_status_bar()
                self._refresh_context_usage()
                await self._completion.refresh()

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
        self._finish_manual_activity_if_running("cancelled" if event.state == WorkerState.CANCELLED else "failed")
        self._compaction_worker = None
        self._manual_compaction_id = None
        self._is_streaming = False
        self._exit_flow.work_finished()
        self._status_hint = "压缩已停止" if event.state == WorkerState.CANCELLED else "压缩未完成"
        if not self._ui_closing and self.is_running:
            composer = self.query_one(ComposerTextArea)
            composer.disabled = False
            composer.focus()
            self._refresh_status_bar()
            await self._completion.refresh()


    async def _restore_session_view(self) -> None:
        chat_view = self._transcript
        await self._transcript.restore()
        self._chat_started = bool(self._session_controller.state.messages or self._transcript.compaction_widgets)
        self.query_one(BannerWidget).set_compact(self._chat_started)
        chat_view.set_class(self._chat_started, "-banner-collapsed")
        self._refresh_phase_chrome()
        self._refresh_composer_placeholder()
        self._refresh_status_bar()
        self._refresh_context_usage()
        self._refresh_plan_panel()
        await self._refresh_pending_queue()
        chat_view.scroll_end(animate=False)

    def _handle_settings_result(self, result: SettingsResult | None) -> None:
        if result is not None and result.saved:
            try:
                if result.config is not None and not result.runtime_applied:
                    fallback = self._turn_runner.reload_models(result.config)
                    if fallback:
                        self.notify(self._turn_runner.model_notice, title="模型")
                if result.config is not None:
                    self._apply_ui_settings(result.config.ui)
            except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                self.notify(f"设置已保存，当前模型更新失败：{exc}", title="模型", severity="error", timeout=10)
                self._status_hint = "模型更新失败"
            else:
                self._status_hint = "设置已保存 · MCP 待应用" if result.mcp_pending else "设置已保存"
        else:
            self._status_hint = "就绪"
        self._refresh_status_bar()
        self._refresh_context_usage()
        self.call_later(self._completion.refresh)
        self.query_one("#composer-input", ComposerTextArea).focus()


    def _handle_model_selected(self, model_ref: str | None) -> str | None:
        if model_ref is not None:
            try:
                self._turn_runner.switch_model(model_ref)
            except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                self.notify(str(exc), title="切换模型失败", severity="error", timeout=10)
            else:
                config = self._turn_runner.model_config
                label = model_display_name(config.providers, model_ref)
                self._status_hint = "就绪"
                self.notify(f"已切换为 {label}", title="模型")
                self._refresh_status_bar()
                self._refresh_context_usage()
        if len(self.screen_stack) == 1:
            self.query_one("#composer-input", ComposerTextArea).focus()
        self.call_later(self._completion.refresh)
        return self._turn_runner.model_ref

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
        settings_service: SettingsService | None = None,
    ) -> None:
        self._app = LanCherTextualApp(
            turn_runner=turn_runner,
            provider_config=provider_config,
            session_controller=session_controller,
            ui_config=ui_config,
            settings_service=settings_service,
        )

    async def run(self) -> int:
        result = await self._app.run_async()
        return 0 if result is None else result

    @property
    def stopped_process_count(self) -> int:
        return self._app._shutdown_process_count
