from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Static

from lancher_code.models import MessageUsage, StreamEvent, TraceEntry, TurnEvent, UIConfig
from lancher_code.tui_views.chat_controls import (
    ChatAction, PendingInputEditor, PendingQueue, PermissionPolicyScreen,
    PlanReviewScreen, ReadOnlyDetailsScreen,
)
from lancher_code.tui_views.composer import ComposerSubmitted, ComposerTextArea
from lancher_code.tui_views.message import MessageWidget, ThinkingTraceWidget, ToolActivityWidget
from lancher_code.tui_views.permission import InlinePermissionPanel, PermissionScopesScreen
from lancher_code.tui_views.theme import theme_palette
from test_tui_flow import FakeProvider, _build_app, _submit_message
from test_tui_permissions import _build_app as permission_app, _permission_request_responses


class GatedProvider:
    """用事件控制回复结束，避免依赖短延迟猜测忙闲。"""
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def stream_chat(self, request):
        self.requests.append(request)
        yield StreamEvent(kind="message_start")
        if len(self.requests) == 1:
            yield StreamEvent(kind="text_delta", text="正在调查")
            self.started.set()
            await self.release.wait()
        yield StreamEvent(kind="text_delta", text="完成")
        yield StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=2, output_tokens=1))


@pytest.mark.asyncio
async def test_busy_enter_queues_then_runs_next_turn_without_overwriting_draft(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, session = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "当前任务")
        await provider.started.wait()
        composer = app.query_one(ComposerTextArea)
        assert not composer.disabled
        composer.text = "下一项任务"
        await pilot.press("enter")
        assert composer.text == ""
        assert app._turn_runner.pending_inputs[0].delivery == "follow_up"
        composer.text = "还没写完的草稿"
        provider.release.set()
        await pilot.pause(0.25)
        assert len(provider.requests) == 2
        assert [m.content for m in session.state.messages if m.role == "user"] == ["当前任务", "下一项任务"]
        assert composer.text == "还没写完的草稿"
        assert not app._turn_runner.pending_inputs


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["steer", "draft"])
async def test_busy_enter_honors_configured_action(openai_provider_config, tmp_path, action):
    provider = GatedProvider()
    app, session = _build_app(provider, openai_provider_config, UIConfig(busy_enter_action=action), tmp_path)
    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "调查当前逻辑")
        await provider.started.wait()
        composer = app.query_one(ComposerTextArea)
        composer.text = "补充边界条件"
        await pilot.press("enter")
        if action == "draft":
            assert composer.text == "补充边界条件"
            assert not app._turn_runner.pending_inputs
        else:
            assert composer.text == ""
            assert app._turn_runner.pending_inputs[0].delivery == "steer"
        provider.release.set()
        await pilot.pause(0.2)
        assert len(provider.requests) == (2 if action == "steer" else 1)


@pytest.mark.asyncio
async def test_stop_pauses_pending_queue_and_preserves_composer(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, _ = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "当前任务")
        await provider.started.wait()
        composer = app.query_one(ComposerTextArea)
        await app.handle_input_submitted(ComposerSubmitted(composer, "后续任务", "follow_up"))
        composer.text = "草稿仍在"
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert not app._is_streaming
        assert app._turn_runner.queue_paused
        assert app._turn_runner.pending_inputs[0].text == "后续任务"
        assert composer.text == "草稿仍在"
        assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_queue_edit_remove_and_convert_preserve_input(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, _ = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "当前任务")
        await provider.started.wait()
        item = app._turn_runner.enqueue_input("随后执行")
        composer = app.query_one(ComposerTextArea)
        composer.text = "独立草稿"
        await app.handle_chat_action(ChatAction("queue-edit", item.id))
        await pilot.pause()
        assert isinstance(app.screen, PendingInputEditor)
        assert app._turn_runner.queue_paused
        app.screen.dismiss("编辑后的任务")
        await pilot.pause()
        assert app._turn_runner.pending_inputs[0].text == "编辑后的任务"
        assert composer.text == "独立草稿"
        await app.handle_chat_action(ChatAction("queue-convert", item.id))
        assert app._turn_runner.pending_inputs[0].delivery == "steer"
        await app.handle_chat_action(ChatAction("queue-delete", item.id))
        assert not app._turn_runner.pending_inputs
        provider.release.set()
        await pilot.pause()


@pytest.mark.asyncio
async def test_ctrl_c_while_permission_waiting_closes_panel_and_preserves_draft(openai_provider_config, tmp_path):
    provider = FakeProvider(_permission_request_responses())
    app, _ = permission_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "运行命令")
        await pilot.pause(0.1)
        assert list(app.query(InlinePermissionPanel))
        composer = app.query_one(ComposerTextArea)
        composer.text = "审批时保留的草稿"
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert not list(app.query(InlinePermissionPanel))
        assert not app._is_streaming
        assert not app._turn_runner.has_active_turn
        assert composer.text == "审批时保留的草稿"


@pytest.mark.asyncio
async def test_tool_activity_remains_visible_when_thinking_is_hidden(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(show_thinking_status=False), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [TraceEntry(kind="thinking", text="内部分析"), TraceEntry(kind="tool_call", tool_name="read_file")]
    async with app.run_test():
        await app._mount_message_widget(message)
        widget = app._message_widgets[message.id]
        assert not widget.query_one(ThinkingTraceWidget).display
        assert widget.query_one(ToolActivityWidget).display


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_native_layout_keeps_composer_and_phase_inside_terminal(openai_provider_config, tmp_path, theme, size):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(theme=theme), tmp_path)
    async with app.run_test(size=size):
        composer = app.query_one("#composer")
        assert composer.region.bottom <= size[1]
        assert composer.region.right <= size[0]
        assert composer.region.height >= 2
        assert app.query_one("#phase-execute").region.right <= size[0]
        assert app.theme == f"lancher-{theme}"
        assert app.screen.styles.background.hex.lower() == theme_palette(theme)["background"]
        hud = str(app.query_one("#status-left" if size[0] < 64 else "#status-center").render())
        assert "预计 " in hud
        assert "就绪" in hud
        if size[0] < 64:
            assert "Enter 发送" in hud
        assert app.query_one("#status-bar").region.bottom <= size[1]


@pytest.mark.asyncio
async def test_plan_execution_uses_snapshot_and_keeps_existing_draft(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, session = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    session.set_work_phase("plan")
    snapshot = session.set_plan_snapshot("1. 修改格式化函数\n2. 验证输出", source_message_id="plan-source", ready=True)
    async with app.run_test() as pilot:
        composer = app.query_one(ComposerTextArea)
        composer.text = "准备写下一条"
        app._handle_plan_accepted((session.session_id, snapshot.digest))
        await provider.started.wait()
        assert session.work_phase == "execute"
        assert composer.text == "准备写下一条"
        assert "修改格式化函数" in session.state.messages[0].content
        provider.release.set()
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_user_scroll_position_is_kept_on_stream_delta(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        for index in range(20):
            message = session.create_user_message(f"历史内容 {index}\n第二行")
            await app._mount_message_widget(message)
        message = session.create_assistant_message()
        await app._mount_message_widget(message)
        await pilot.pause()
        view = app.query_one("#chat-view")
        view.scroll_home(animate=False)
        await pilot.pause()
        assert not view.is_vertical_scroll_end
        session.append_message_content(message.id, "新的流式输出")
        await app._consume_turn_event(TurnEvent(kind="assistant_text_delta", message=message, text="新的流式输出"))
        await pilot.pause()
        assert view.scroll_y == 0


@pytest.mark.asyncio
async def test_narrow_permission_and_queue_leave_editor_and_hud_visible(openai_provider_config, tmp_path):
    app, _ = permission_app(FakeProvider(_permission_request_responses()), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test(size=(32, 16)) as pilot:
        await _submit_message(app, pilot, "检查保存和取消行为")
        await pilot.pause(0.1)
        panel = app.query_one(InlinePermissionPanel)
        command = "python -m pytest tests/test_settings.py tests/test_config_bootstrap.py -q"
        panel.request.command = command
        panel.query_one("#permission-command", Static).update(command)
        panel.query_one("#permission-description", Static).update("检查保存、取消和返回行为。" * 5)
        composer = app.query_one(ComposerTextArea)
        composer.text = "保留长草稿\n第二行\n第三行"
        app._turn_runner.enqueue_input("下一轮再检查文件")
        await app._refresh_pending_queue()
        await pilot.pause()
        assert app.query_one("#composer").region.bottom <= 16
        assert app.query_one("#status-bar").region.bottom <= 16
        assert app.query_one("#phase-execute").region.bottom <= 16
        assert app.query_one(PendingQueue).region.bottom <= app.query_one("#composer").region.y
        assert len(panel._options()) == 2
        hud = str(app.query_one("#status-left").render())
        assert "等待确认" in hud
        assert "预计 " in hud
        assert "Enter 排队" in hud
        for target in ("#chat-steer", "#chat-queue"):
            assert app.query_one(target).region.right <= 32
            assert app.query_one(target).region.bottom <= 16
        for target in ("#permission-primary", "#permission-compact-actions"):
            region = panel.query_one(target).region
            assert region.y >= panel.region.y
            assert region.bottom <= panel.region.bottom
            assert region.right <= 32
        panel.focus()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ReadOnlyDetailsScreen)
        assert command in app.screen.text
        assert str(tmp_path) in app.screen.text
        assert app.screen.query_one("#read-only-close").region.bottom <= 16
        await pilot.press("escape")
        panel.focus()
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, PermissionScopesScreen)
        assert panel.request.session_rule in str(app.screen.query_one("#scope-session-rule").render())
        project_button = app.screen.query_one("#scope-project", Button)
        for _ in range(8):
            if app.focused is project_button:
                break
            await pilot.press("tab")
        await pilot.pause()
        assert app.focused is project_button
        assert project_button.region.bottom <= 16
        await pilot.press("escape")
        panel.focus()
        await pilot.press("i")
        await pilot.pause()
        assert app.focused is composer
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert composer.text == "保留长草稿\n第二行\n第三行"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plan", "policy", "queue"])
async def test_narrow_dialog_actions_are_keyboard_reachable(openai_provider_config, tmp_path, kind):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    snapshot = session.set_plan_snapshot("计划明细\n" * 40, source_message_id="x", ready=True)
    async with app.run_test(size=(32, 16)) as pilot:
        if kind == "plan":
            screen = PlanReviewScreen(session.session_id, snapshot, can_execute=True)
            target = "#review-execute"
        elif kind == "policy":
            screen = PermissionPolicyScreen()
            target = "#policy-bypass"
        else:
            screen = PendingInputEditor("草稿\n" * 40)
            target = "#queue-editor-save"
        app.push_screen(screen)
        await pilot.pause()
        # 焦点切换应滚动到动作控件，而不是让它留在终端外。
        button = screen.query_one(target, Button)
        for _ in range(12):
            if app.focused is button:
                break
            await pilot.press("tab")
        await pilot.pause()
        assert app.focused is button
        assert button.region.y >= 0
        assert button.region.bottom <= 16
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not screen


@pytest.mark.asyncio
@pytest.mark.parametrize("details_key", ["d", "m"])
async def test_cancel_closes_the_active_permission_details_screen(openai_provider_config, tmp_path, details_key):
    app, _ = permission_app(FakeProvider(_permission_request_responses()), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test(size=(32, 16)) as pilot:
        await _submit_message(app, pilot, "等待确认")
        await pilot.pause()
        app.query_one(InlinePermissionPanel).focus()
        await pilot.press(details_key)
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert not list(app.query(InlinePermissionPanel))
        assert not app._turn_runner.has_active_turn


@pytest.mark.asyncio
async def test_discussion_context_estimate_preserves_phase(openai_provider_config, tmp_path, monkeypatch):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.set_work_phase("discuss")
    estimates = []
    original = session.estimate_request_tokens

    def record(request):
        estimates.append(request.work_phase)
        return original(request)

    monkeypatch.setattr(session, "estimate_request_tokens", record)
    async with app.run_test():
        assert estimates
        assert set(estimates) == {"discuss"}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (160, 40)])
async def test_long_model_hud_keeps_phase_and_policy_when_status_width_changes(openai_provider_config, tmp_path, size):
    openai_provider_config.model = "用于复杂代码任务的长中文模型名称" * 4
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.set_work_phase("plan")
    async with app.run_test(size=size) as pilot:
        for hint in ("就绪", "等待确认 · 可继续编辑草稿", "就绪"):
            app._status_hint = hint
            app._refresh_status_bar()
            await pilot.pause()
            label = app.query_one("#status-left", Static)
            text = label.render().plain
            assert "计划 · 逐次确认" in text
            assert cell_len(text) <= label.region.width


@pytest.mark.asyncio
async def test_narrow_inline_plan_keeps_both_actions_inside_view(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.set_work_phase("plan")
    session.set_plan_snapshot("1. 调整保存行为\n2. 验证取消", source_message_id="plan-source", ready=True)
    async with app.run_test(size=(32, 16)) as pilot:
        await pilot.pause()
        for target in ("#plan-review", "#plan-execute"):
            button = app.query_one(target, Button)
            assert button.region.right <= 32
            assert button.region.bottom <= 16
        app.query_one("#plan-review", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PlanReviewScreen)
