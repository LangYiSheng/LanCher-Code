from __future__ import annotations

import asyncio
import pytest
from rich.cells import cell_len
from textual.widgets import DataTable, Input, Static

from lancher_code.models import UIConfig
from lancher_code.tui_views.tasks import TaskScreenActions, TasksScreen
from lancher_code.tui_views.composer import ComposerTextArea, ComposerSubmitted
from test_tui_flow import FakeProvider, _build_app, _submit_message
from test_tui_native import GatedProvider


class FakeTaskRuntime:
    def __init__(self):
        self.process_id = "12345678123442348123456781234567"
        self.task = {"process_id": self.process_id, "status": "running", "lifetime": "turn",
                     "description": "开发服务器", "command": "serve", "cwd": "项目"}
        self.calls = []

    def list_tasks(self):
        return [dict(self.task)]

    async def read(self, process_id, cursor):
        self.calls.append(("read", process_id, cursor))
        return {"output": "服务已就绪\n" if cursor == 0 else "", "next_cursor": 100, "has_more": False}

    async def stop(self, process_id):
        self.calls.append(("stop", process_id))
        self.task.update(status="exited", exit_code=1, exit_reason="用户停止")

    async def background(self, process_id):
        self.calls.append(("background", process_id))
        self.task["lifetime"] = "session"

    async def write(self, process_id, text):
        self.calls.append(("write", process_id, text))
        self.task.update(input_status="sent", input_bytes=len(text.encode("utf-8")))

    async def stop_session(self):
        self.calls.append(("stop-session",))

    def actions(self):
        return TaskScreenActions(self.list_tasks, self.read, self.stop, self.background, self.write, self.stop_session)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_task_screen_reads_independent_cursor_and_keeps_owner(openai_provider_config, tmp_path, size):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    runtime = FakeTaskRuntime()
    owner = "87654321876543218765432187654321"
    screen = TasksScreen(owner, runtime.actions())
    async with app.run_test(size=size) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        assert screen.session_id == owner
        assert app.screen.query_one(DataTable).row_count == 1
        assert "服务已就绪" in str(screen.query_one("#tasks-output", Static).render())
        assert screen.query_one("#tasks-output", Static).region.height <= screen.query_one("#tasks-output-scroll").region.height
        await screen.refresh_tasks()
        assert runtime.calls[:2] == [("read", runtime.process_id, 0), ("read", runtime.process_id, 100)]
        assert screen.query_one("#tasks-box").region.right <= size[0]
        assert screen.query_one("#tasks-close").region.bottom <= size[1]
        screen.action_close()
        await pilot.pause()
        assert session.session_id is None
        assert all(call[0] not in {"stop", "stop-session"} for call in runtime.calls)


@pytest.mark.asyncio
async def test_task_actions_send_exact_line_transfer_and_stop(openai_provider_config, tmp_path):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    runtime = FakeTaskRuntime()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = TasksScreen("87654321876543218765432187654321", runtime.actions())
        app.push_screen(screen)
        await pilot.pause()
        await pilot.click("#tasks-background")
        await pilot.pause()
        assert ("background", runtime.process_id) in runtime.calls
        assert runtime.task["lifetime"] == "session"
        screen.query_one(Input).value = "  中文输入  "
        await pilot.click("#tasks-input-send")
        await pilot.pause()
        assert ("write", runtime.process_id, "  中文输入  \n") in runtime.calls
        assert screen.query_one(Input).value == ""
        assert "输入：已接收 · 累计 17 字节" in str(screen.query_one("#tasks-detail", Static).render())
        await pilot.click("#tasks-stop")
        await pilot.pause()
        assert ("stop", runtime.process_id) in runtime.calls
        assert "退出码" in str(screen.query_one("#tasks-detail", Static).render())
        await pilot.click("#tasks-stop-session")
        await pilot.pause()
        assert ("stop-session",) in runtime.calls


@pytest.mark.asyncio
async def test_busy_tasks_command_opens_and_escape_scope_is_local(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, session = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await _submit_message(app, pilot, "当前回答")
        await provider.started.wait()
        composer = app.query_one(ComposerTextArea)
        composer.text = "/tasks"
        await app.handle_input_submitted(ComposerSubmitted(composer, composer.text))
        await pilot.pause()
        assert isinstance(app.screen, TasksScreen) and app._turn_runner.has_active_turn
        assert app.screen.session_id == session.session_id
        await pilot.press("escape")
        assert len(app.screen_stack) == 1 and app._turn_runner.has_active_turn
        composer.text = "停止后还要保留的草稿"
        composer.focus()
        await pilot.press("escape")
        await pilot.pause()
        assert not app._turn_runner.has_active_turn
        assert composer.text == "停止后还要保留的草稿"


@pytest.mark.asyncio
async def test_busy_session_stop_preserves_queue_and_clears_command(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, _ = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await _submit_message(app, pilot, "当前回答")
        await provider.started.wait()
        composer = app.query_one(ComposerTextArea)
        await app.handle_input_submitted(ComposerSubmitted(composer, "后续消息", "follow_up"))
        composer.text = "/session stop"
        await app.handle_input_submitted(ComposerSubmitted(composer, composer.text))
        await pilot.pause()
        assert not app._turn_runner.has_active_turn
        assert app._turn_runner.queue_paused
        assert app._turn_runner.pending_inputs[0].text == "后续消息"
        assert composer.text == ""


@pytest.mark.asyncio
async def test_task_screen_facade_binds_session_before_later_switch(openai_provider_config, tmp_path, monkeypatch):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.create_user_message("原对话")
    owner = session.session_id
    runtime = FakeTaskRuntime()
    seen = []
    def list_processes(session_id=None):
        seen.append(("list", session_id))
        return runtime.list_tasks()
    def read_output(process_id, cursor=0, max_chars=16000, session_id=None):
        seen.append(("read", session_id))
        return {"text": "原对话日志" if cursor == 0 else "", "next_cursor": 100}
    async def stop(process_id, session_id=None):
        seen.append(("stop", session_id))
    monkeypatch.setattr(app._turn_runner, "list_processes", list_processes)
    monkeypatch.setattr(app._turn_runner, "read_process_output", read_output)
    monkeypatch.setattr(app._turn_runner, "stop_process", stop)
    async with app.run_test(size=(100, 40)) as pilot:
        await app._execute_tasks_command(f"show {runtime.process_id}")
        await pilot.pause()
        app._turn_runner.new_session()
        assert session.session_id is None
        await pilot.click("#tasks-stop")
        await pilot.pause()
        assert ("stop", owner) in seen
        assert all(session_id == owner for _, session_id in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_hud_keeps_background_and_notification_visible(openai_provider_config, tmp_path, monkeypatch, size):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    counts = {"background": 2, "running": 2, "notifications": 1, "waiting": 3}
    monkeypatch.setattr(app._turn_runner, "execution_summary", lambda session_id=None: counts)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        selector = "#status-right" if size[0] >= 64 else "#status-left"
        widget = app.query_one(selector, Static)
        visible_status = str(widget.render())
        assert "后台 2" in visible_status
        assert "通知 1 /tasks" in visible_status
        if size[0] < 64:
            assert all(cell_len(line) <= size[0] - 4 for line in widget.content.plain.splitlines())
        assert widget.region.right <= size[0]
        details = str(app.query_one("#status-details", Static).render())
        assert "托管进程：运行 2 · 会话后台 2 · 排队 3" in details
        assert "未读完成通知：1 · /tasks 查看任务与输出" in details
        counts.update(background=0, notifications=0)
        app._refresh_status_bar()
        assert "后台" not in str(widget.render())
        assert "通知" not in str(widget.render())


@pytest.mark.asyncio
async def test_hud_timer_stops_before_slow_runtime_shutdown(openai_provider_config, tmp_path, monkeypatch):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    shutdown_started = asyncio.Event()
    refreshes_during_shutdown = []
    refresh = app._refresh_status_bar
    shutdown = app._turn_runner.shutdown

    def observe_refresh():
        if shutdown_started.is_set():
            refreshes_during_shutdown.append(True)
        refresh()

    async def slow_shutdown():
        shutdown_started.set()
        # DOM 此时已卸载；迟到回调也必须安全返回。
        refresh()
        # 超过真实一秒周期，确保不会有定时刷新追进异步资源清理。
        await asyncio.sleep(1.1)
        await shutdown()

    monkeypatch.setattr(app, "_refresh_status_bar", observe_refresh)
    monkeypatch.setattr(app._turn_runner, "shutdown", slow_shutdown)
    async with app.run_test(size=(32, 16)) as pilot:
        await pilot.pause()
    assert shutdown_started.is_set()
    assert not refreshes_during_shutdown


@pytest.mark.asyncio
async def test_hud_refresh_ignores_already_unmounted_widgets(openai_provider_config, tmp_path):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.query_one("#status-bar").remove()
        app._refresh_status_bar()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_blocker_task_selection_keeps_full_identity_and_stops_only_target(
    openai_provider_config, tmp_path, size,
):
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    runtime = FakeTaskRuntime()
    other = dict(runtime.task, process_id="00000000000040008000000000000000", description="无关任务")
    actions = runtime.actions()
    actions = TaskScreenActions(lambda: [other, runtime.list_tasks()[0]], actions.read_output,
        actions.stop, actions.background, actions.write_input, actions.stop_session)
    screen = TasksScreen("87654321876543218765432187654321", actions,
                         selected_process_id=runtime.process_id)
    async with app.run_test(size=size) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        assert screen.selected_process_id == runtime.process_id
        detail = screen.query_one("#tasks-detail", Static)
        assert runtime.process_id in detail.content
        if size[0] < 64:
            assert detail.content == runtime.process_id
            assert detail.get_content_height(detail.container_size, app.screen.size, detail.size.width) <= detail.region.height
        await pilot.click("#tasks-stop")
        await pilot.pause()
        assert ("stop", runtime.process_id) in runtime.calls
        assert ("stop", other["process_id"]) not in runtime.calls


