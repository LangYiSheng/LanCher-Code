"""窄终端优先显示退出提示和草稿，补全在继续操作或超时后恢复。"""
from __future__ import annotations

import asyncio

import pytest

from lancher_code.models import StreamEvent, UIConfig
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tui_views.chat import LanCherTextualApp
from lancher_code.tui_views.composer import ComposerTextArea
from lancher_code.tui_views.exit_flow import ExitFlow
from lancher_code.turn_runner import TurnRunner


class IdleProvider:
    async def stream_chat(self, request):
        yield StreamEvent(kind="message_end")


@pytest.mark.parametrize("session_menu", [False, True])
@pytest.mark.parametrize("paused_queue", [False, True])
async def test_exit_hint_with_background_count_keeps_tiny_terminal_controls_visible(
    openai_provider_config, tmp_path, monkeypatch, session_menu, paused_queue,
):
    session = SessionController(openai_provider_config, cwd=tmp_path)
    session.create_user_message("第一段会话，用来提供恢复候选")
    session.new_session()
    session.create_user_message("第二段会话，退出时仍然保留")
    registry = create_default_tool_registry()
    runner = TurnRunner(IdleProvider(), session, registry, ToolExecutor(registry, cwd=tmp_path))
    app = LanCherTextualApp(runner, openai_provider_config, session, UIConfig())
    clock = [0.0]
    app._exit_flow = ExitFlow(clock=lambda: clock[0])
    # 这里只验证排版，真实进程退出由应用集成测试负责。
    monkeypatch.setattr(TurnRunner, "application_process_count", property(lambda _: 2))
    if paused_queue:
        runner.enqueue_input("随后继续的任务，需要保留在暂停队列里")
        runner.pause_queue()
    try:
        async with asyncio.timeout(15), app.run_test(size=(32, 16)) as pilot:
            await app._restore_session_view()
            composer = app.query_one(ComposerTextArea)
            composer.text = "/session resume " if session_menu else "还没发出的草稿"
            composer.cursor_location = composer.document.end
            await app._refresh_command_ui()
            await pilot.pause()
            widgets = {name: app.query_one("#" + name) for name in (
                "exit-hint", "composer", "composer-input", "composer-actions", "status-bar",
                "composer-region", "slash-command-menu", "command-hint", "pending-queue",
            )}

            def assert_controls_visible(phase):
                regions = {name: (item.region.x, item.region.y, item.region.width, item.region.height)
                           for name, item in widgets.items()}
                for name in ("exit-hint", "composer-input", "composer-actions", "status-bar",
                             "slash-command-menu", "command-hint", "pending-queue"):
                    if widgets[name].display:
                        assert widgets[name].region.y >= 0, (phase, regions)
                        assert widgets[name].region.bottom <= 16, (phase, regions)

            assert_controls_visible("退出确认之前")
            candidate_keys = tuple(item.key for item in app._slash_menu_matches)
            active_key = app._current_active_completion_key()
            await pilot.press("ctrl+c")
            await pilot.pause()

            assert app._exit_flow.is_armed
            assert widgets["exit-hint"].display
            assert "2 个托管进程" in str(widgets["exit-hint"].render())
            if session_menu:
                assert not widgets["slash-command-menu"].display
                assert not widgets["command-hint"].display
                assert tuple(item.key for item in app._slash_menu_matches) == candidate_keys
                assert app._current_active_completion_key() == active_key
            if paused_queue:
                assert widgets["pending-queue"].display and runner.queue_paused
            assert_controls_visible("退出确认中")
            assert widgets["exit-hint"].region.bottom <= widgets["composer"].region.y
            assert composer.text == ("/session resume " if session_menu else "还没发出的草稿")

            await pilot.press("left")
            await pilot.pause()
            assert_controls_visible("方向键解除确认之后")
            assert not app._exit_flow.is_armed and not widgets["exit-hint"].display
            if session_menu:
                assert widgets["slash-command-menu"].display and widgets["command-hint"].display
                assert tuple(item.key for item in app._slash_menu_matches) == candidate_keys
                assert app._current_active_completion_key() == active_key

            # 超时也须恢复菜单；假时钟只改变确认有效期，不等待三秒。
            await pilot.press("ctrl+c")
            clock[0] = 3.0
            app._refresh_exit_hint()
            await pilot.pause()
            assert_controls_visible("确认超时之后")
            assert not app._exit_flow.is_armed and not widgets["exit-hint"].display
            if session_menu:
                assert widgets["slash-command-menu"].display and widgets["command-hint"].display
                assert tuple(item.key for item in app._slash_menu_matches) == candidate_keys
                assert app._current_active_completion_key() == active_key
    finally:
        session.close()
