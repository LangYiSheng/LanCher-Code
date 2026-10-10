"""退出确认须覆盖真实输入焦点、弹窗和可取消的独立压缩任务。"""
from __future__ import annotations

import asyncio

import pytest
from textual import events
from textual.widgets import Static

from lancher_code.contracts.messages import StreamEvent
from lancher_code.config.models import UIConfig
from lancher_code.sessions.controller import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tui.app import LanCherTextualApp
from lancher_code.tui.chat_controls import ReadOnlyDetailsScreen
from lancher_code.tui.composer import ComposerTextArea
from lancher_code.tui.exit_flow import ExitFlow
from lancher_code.agent.runner import TurnRunner


class IdleProvider:
    async def stream_chat(self, request):
        yield StreamEvent(kind="message_end", response_complete=True, assistant_blocks=[])


def build_app(config, directory, provider=None):
    session = SessionController(config, cwd=directory)
    registry = create_default_tool_registry()
    executor = ToolExecutor(registry, cwd=directory)
    runner = TurnRunner(provider or IdleProvider(), session, registry, executor)
    return LanCherTextualApp(runner, config, session, UIConfig()), session, runner


async def until(pilot, predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause(0.01)


@pytest.mark.parametrize("width", [100, 60, 32])
async def test_idle_double_interrupt_is_visible_and_exits(openai_provider_config, tmp_path, width):
    app, session, _ = build_app(openai_provider_config, tmp_path)
    async with app.run_test(size=(width, 16)) as pilot:
        await pilot.press("ctrl+c")
        assert app.is_running and app._exit_flow.is_armed
        hint = app.query_one("#exit-hint", Static)
        assert hint.display and "再按一次" in str(hint.render())
        assert hint.region.bottom <= app.query_one("#composer-actions").region.y
        await pilot.press("ctrl+c")
        assert app._exit_flow.closing
    assert session.session_id is None and session.list_sessions().items == []


async def test_editing_and_timeout_require_a_new_confirmation(openai_provider_config, tmp_path):
    app, _, _ = build_app(openai_provider_config, tmp_path)
    clock = [0.0]
    app._exit_flow = ExitFlow(clock=lambda: clock[0])
    async with app.run_test() as pilot:
        await pilot.press("ctrl+c", "x")
        assert not app._exit_flow.is_armed
        assert app.query_one(ComposerTextArea).text == "x"
        await pilot.press("ctrl+c")
        clock[0] = 3.0
        await pilot.press("ctrl+c")
        assert app.is_running and app._exit_flow.is_armed
        await pilot.press("left")
        assert not app._exit_flow.is_armed


async def test_modal_focus_uses_same_confirmation(openai_provider_config, tmp_path):
    app, _, _ = build_app(openai_provider_config, tmp_path)
    async with app.run_test(size=(32, 16)) as pilot:
        app.push_screen(ReadOnlyDetailsScreen("状态详情"))
        await pilot.pause()
        await pilot.press("ctrl+c")
        assert app._exit_flow.is_armed and app.is_running
        await pilot.press("ctrl+c")
        assert app._exit_flow.closing


@pytest.mark.parametrize("event_type", [
    events.MouseScrollUp, events.MouseScrollDown,
    events.MouseScrollLeft, events.MouseScrollRight,
])
async def test_scrolling_disarms_exit_confirmation(openai_provider_config, tmp_path, event_type):
    app, _, _ = build_app(openai_provider_config, tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+c")
        assert app._exit_flow.is_armed
        app.post_message(event_type(
            widget=None, x=1, y=1, delta_x=0, delta_y=0,
            button=0, shift=False, meta=False, ctrl=False,
        ))
        await until(pilot, lambda: not app._exit_flow.is_armed)
        await pilot.press("ctrl+c")
        assert app.is_running and app._exit_flow.is_armed


async def test_ctrl_d_removed_but_status_details_still_work(openai_provider_config, tmp_path):
    app, _, _ = build_app(openai_provider_config, tmp_path)
    assert all("ctrl+d" not in binding.key.split(",") for binding in ComposerTextArea.BINDINGS)
    async with app.run_test() as pilot:
        composer = app.query_one(ComposerTextArea)
        composer.text = "abc"
        composer.move_cursor((0, 0))
        await pilot.press("ctrl+d")
        assert composer.text == "abc" and not app._details_open
        await app._dispatch_slash_command("status", "")
        assert app._details_open
        assert "Ctrl+D" not in str(app.query_one("#status-right", Static).render())


async def test_stopping_work_does_not_arm_exit_or_repeat_cancel(openai_provider_config, tmp_path):
    class Provider:
        started = asyncio.Event()
        stopping = asyncio.Event()
        release = asyncio.Event()
        cancelled = 0

        async def stream_chat(self, request):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                self.stopping.set()
                await self.release.wait()
                raise
            yield StreamEvent(kind="message_end", response_complete=True, assistant_blocks=[])

    provider = Provider()
    app, _, runner = build_app(openai_provider_config, tmp_path, provider)
    async with app.run_test() as pilot:
        composer = app.query_one(ComposerTextArea)
        composer.text = "当前任务"
        await pilot.press("enter")
        await until(pilot, provider.started.is_set)
        composer.text = "保留草稿"
        await pilot.press("ctrl+c")
        await until(pilot, provider.stopping.is_set)
        await pilot.press("ctrl+c")
        assert provider.cancelled == 1 and not app._exit_flow.is_armed
        provider.release.set()
        await until(pilot, lambda: not app._is_streaming and not runner.has_active_turn)
        assert composer.text == "保留草稿"
        await pilot.press("ctrl+c")
        assert app._exit_flow.is_armed and app.is_running


async def test_manual_compaction_is_cancelled_without_blocking_input_events(openai_provider_config, tmp_path, monkeypatch):
    app, session, runner = build_app(openai_provider_config, tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def compact(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(session, "compact_context", compact)
    async with app.run_test() as pilot:
        composer = app.query_one(ComposerTextArea)
        composer.text = "/compact"
        await pilot.press("enter")
        await until(pilot, started.is_set)
        assert runner._manual_compaction and not runner.has_active_turn
        await pilot.press("ctrl+c")
        await until(pilot, lambda: stopped.is_set() and not app._is_streaming)
        assert not runner._manual_compaction and not app._exit_flow.is_armed
        assert not composer.disabled
        await pilot.press("ctrl+c")
        assert app._exit_flow.is_armed


async def test_runner_shutdown_waits_for_manual_compaction(openai_provider_config, tmp_path, monkeypatch):
    _, session, runner = build_app(openai_provider_config, tmp_path)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def compact(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(session, "compact_context", compact)
    task = asyncio.create_task(runner.compact_context())
    await started.wait()
    await runner.shutdown()
    assert task.done() and task.cancelled() and finished.is_set()
    assert runner._compaction_task is None
    session.close()


async def test_interrupt_before_compaction_worker_starts_recovers_idle(openai_provider_config, tmp_path):
    app, _, _ = build_app(openai_provider_config, tmp_path)
    async with app.run_test() as pilot:
        await app._dispatch_slash_command("compact", "")
        # 不让出事件循环，直接覆盖 worker 尚未进入协程的取消窗口。
        await app.action_request_quit()
        await until(pilot, lambda: not app._is_streaming)
        assert app._compaction_worker is None and not app.query_one(ComposerTextArea).disabled
        assert not app._exit_flow.is_armed


async def test_initialization_can_exit_with_double_interrupt(openai_provider_config, tmp_path):
    app, _, runner = build_app(openai_provider_config, tmp_path)

    class MCP:
        has_servers = True
        started = asyncio.Event()
        cancelled = asyncio.Event()

        def add_progress_callback(self, callback):
            pass

        async def initialize(self, registry):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    manager = MCP()
    app._mcp_manager = manager
    app._tool_registry = runner._tool_registry
    app.mcp_initialization_complete = False
    async with app.run_test(size=(32, 16)) as pilot:
        await until(pilot, manager.started.is_set)
        assert app.query_one(ComposerTextArea).disabled
        await pilot.press("ctrl+c")
        assert app._exit_flow.is_armed
        await pilot.press("ctrl+c")
    assert manager.cancelled.is_set()
