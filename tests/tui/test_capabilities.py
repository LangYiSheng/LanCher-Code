from __future__ import annotations

import asyncio
import inspect

import pytest
from textual.widgets import Button, DataTable, Static

from lancher_code.agent.skills import SkillsService
from lancher_code.config.models import UIConfig
from lancher_code.mcp.manager import MCPInitializationProgress, MCPServerStatus
from lancher_code.tui.capabilities import CapabilitiesScreen
from lancher_code.tui.app import ChatTUI, LanCherTextualApp
from lancher_code.tui.chat_controls import ReadOnlyDetailsScreen
from lancher_code.tui.composer import ComposerSubmitted, ComposerTextArea
from lancher_code.tui.settings.mcp import MCPSettingsEditor
from test_settings import _service
from test_tui_commands import type_command
from test_tui_flow import FakeProvider, _build_app
from test_tui_native import GatedProvider


def test_tui_constructors_only_receive_core_runtime_and_presentation_dependencies():
    for app_class in (ChatTUI, LanCherTextualApp):
        parameters = inspect.signature(app_class.__init__).parameters
        assert "mcp_manager" not in parameters and "tool_registry" not in parameters


class SnapshotManager:
    def __init__(self) -> None:
        self.callbacks = []
        self.calls = []
        self.refresh_started = asyncio.Event()
        self.refresh_release = asyncio.Event()
        self.refresh_release.set()

    def add_progress_callback(self, callback):
        self.callbacks.append(callback)

    def status(self):
        return (MCPServerStatus("demo", "ready", "stdio", 2),)

    def publish(self, state):
        for callback in self.callbacks:
            callback(MCPInitializationProgress(1, 1, 1, 0, 2, None, state))

    async def initialize(self, registry):
        self.publish("complete")

    async def refresh(self, registry, server_name):
        self.calls.append(("refresh", server_name))
        self.refresh_started.set()
        await self.refresh_release.wait()
        self.publish("catalog_updated")
        return self.status()

    async def reconnect(self, registry, server_name):
        self.calls.append(("reconnect", server_name))
        self.publish("reconnected")
        return self.status()

    async def close(self):
        pass


def build_capability_app(config, tmp_path, provider=None):
    skill_dir = tmp_path / ".lancher" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: 审查代码与验证行为\n---\n先调查调用关系，再报告可操作的问题。\n",
        encoding="utf-8",
    )
    skills = SkillsService(tmp_path, user_root=tmp_path / "profile")
    app, session = _build_app(provider or FakeProvider([]), config, UIConfig(), tmp_path, skills_service=skills)
    manager = SnapshotManager()
    app._turn_runner.configure_capabilities(mcp_manager=manager)
    return app, session, manager


async def finish_operation(screen, pilot):
    async with asyncio.timeout(10):
        while (screen._working or str(screen.query_one("#capability-notice", Static).render()) == ""
               or any(button.has_class("-active") for button in screen.query(Button))):
            await pilot.pause(0.01)
    await pilot.pause()


@pytest.mark.asyncio
async def test_skill_listing_details_and_management_use_core_without_creating_turn(openai_provider_config, tmp_path):
    app, session, _ = build_capability_app(openai_provider_config, tmp_path)
    async with app.run_test() as pilot:
        await app._execute_slash_command("skills", "list")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CapabilitiesScreen)
        assert screen.query_one(DataTable).row_count == 1
        key = screen.selected_id
        assert key == "project/review"
        await pilot.press("enter")
        assert isinstance(app.screen, ReadOnlyDetailsScreen)
        assert "先调查调用关系" in app.screen.text and "来源：project" in app.screen.text
        assert not session.context_state.skill_activations
        await pilot.press("escape")
        await pilot.click("#capability-primary")
        await finish_operation(screen, pilot)
        assert session.context_state.disabled_skills == [key]
        assert str(screen.query_one("#capability-primary", Button).label) == "启用"
        screen.query_one("#capability-primary", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not session.context_state.disabled_skills
        await pilot.press("escape")
        await app._execute_slash_command("skills", "reload")
        await app.workers.wait_for_complete()
        assert session.session_id is None and not session.state.messages


@pytest.mark.asyncio
async def test_mcp_commands_route_to_core_and_list_refresh_remains_responsive(openai_provider_config, tmp_path):
    app, session, manager = build_capability_app(openai_provider_config, tmp_path)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/mcp reconnect de")
        await pilot.press("tab", "enter")
        await app.workers.wait_for_complete()
        assert manager.calls == [("reconnect", "demo")]
        assert composer.text == "" and session.session_id is None
        await app._execute_slash_command("mcp", "refresh")
        await app.workers.wait_for_complete()
        assert manager.calls[-1] == ("refresh", None)
        await app._execute_slash_command("mcp", "list")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CapabilitiesScreen)
        assert screen.query_one(DataTable).get_row("demo")[2] == "2"
        await pilot.click("#capability-show")
        assert isinstance(app.screen, ReadOnlyDetailsScreen) and "已连接" in app.screen.text
        await pilot.press("escape")
        manager.refresh_release.clear()
        manager.refresh_started.clear()
        await pilot.click("#capability-primary")
        await manager.refresh_started.wait()
        assert screen._working
        await pilot.press("escape")
        assert len(app.screen_stack) == 1
        manager.refresh_release.set()


@pytest.mark.asyncio
async def test_text_mcp_refresh_keeps_message_pump_responsive_until_server_returns(openai_provider_config, tmp_path):
    app, session, manager = build_capability_app(openai_provider_config, tmp_path)
    manager.refresh_release.clear()
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/mcp refresh demo")
        try:
            async with asyncio.timeout(3):
                await pilot.press("enter")
                await manager.refresh_started.wait()
            assert app._turn_runner.capabilities.updating
            assert composer.text == "/mcp refresh demo" and not composer.disabled
            # 真实键盘事件必须在服务器尚未返回时被主消息循环处理。
            await pilot.press("ctrl+c")
            assert app.is_running and app._exit_flow.is_armed
            await type_command(app, pilot, "/status")
            await pilot.press("tab", "enter")
            assert app._details_open and composer.text == ""
            await type_command(app, pilot, "下一条草稿")
            await pilot.press("x")
            assert composer.text == "下一条草稿x"
            assert not manager.refresh_release.is_set()
        finally:
            manager.refresh_release.set()
            await app.workers.wait_for_complete()
        assert composer.text == "下一条草稿x"
        assert not app._turn_runner.capabilities.updating
        assert session.session_id is None and not session.state.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("new_draft", [None, "用户后来输入的草稿"])
async def test_text_mcp_failure_preserves_original_or_newer_draft(openai_provider_config, tmp_path, monkeypatch, new_draft):
    app, session, manager = build_capability_app(openai_provider_config, tmp_path)
    manager.refresh_release.clear()
    notices = []

    async def fail_refresh(registry, server_name):
        manager.refresh_started.set()
        await manager.refresh_release.wait()
        raise OSError("服务器暂时不可用")

    monkeypatch.setattr(manager, "refresh", fail_refresh)
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append((message, kwargs)))
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/mcp refresh demo")
        try:
            async with asyncio.timeout(3):
                await pilot.press("enter")
                await manager.refresh_started.wait()
            if new_draft is not None:
                await type_command(app, pilot, new_draft)
        finally:
            manager.refresh_release.set()
            await app.workers.wait_for_complete()
        assert composer.text == (new_draft or "/mcp refresh demo")
        assert notices[-1][0] == "服务器暂时不可用"
        assert notices[-1][1]["title"] == "命令未执行"
        assert not app._turn_runner.capabilities.updating
        assert session.session_id is None and not session.state.messages


@pytest.mark.asyncio
async def test_busy_readonly_listing_is_available_and_mutations_preserve_draft(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, session, manager = build_capability_app(openai_provider_config, tmp_path, provider)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "调查当前任务")
        await pilot.press("enter")
        await provider.started.wait()
        await type_command(app, pilot, "/mcp reload")
        await app.handle_input_submitted(ComposerSubmitted(composer, composer.text))
        assert composer.text == "/mcp reload" and not manager.calls
        await app._execute_slash_command("skills", "list")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, CapabilitiesScreen)
        await pilot.click("#capability-primary")
        await finish_operation(screen, pilot)
        assert not session.context_state.disabled_skills
        assert "等待完成" in str(screen.query_one("#capability-notice", Static).render())
        await pilot.press("escape")
        provider.release.set()
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skills", "mcp"])
@pytest.mark.parametrize("size", [(100, 40), (32, 16)])
async def test_capability_lists_keep_rows_and_actions_reachable(openai_provider_config, tmp_path, kind, size):
    app, _, _ = build_capability_app(openai_provider_config, tmp_path)
    async with app.run_test(size=size) as pilot:
        await app._execute_slash_command(kind, "list")
        await pilot.pause()
        screen = app.screen
        table = screen.query_one(DataTable)
        assert table.row_count == 1 and table.content_region.height >= 3
        for button in screen.query(Button):
            assert button.region.y >= 0 and button.region.bottom <= size[1]
            assert button.region.x >= 0 and button.region.right <= size[0]
        await pilot.press("enter")
        assert isinstance(app.screen, ReadOnlyDetailsScreen)
        close = app.screen.query_one("#read-only-close")
        assert close.region.bottom <= size[1]


@pytest.mark.asyncio
async def test_settings_mcp_save_awaits_core_reload_and_reports_application_failure(openai_provider_config, tmp_path, monkeypatch):
    app, _, _ = build_capability_app(openai_provider_config, tmp_path)
    app._settings_service = _service(tmp_path)
    reloads = []

    async def reload_mcp():
        reloads.append("reload")
        return "MCP 配置已应用。"

    monkeypatch.setattr(app._turn_runner.capabilities, "reload_mcp", reload_mcp)
    async with app.run_test() as pilot:
        await app._execute_slash_command("settings", "open")
        await pilot.pause()
        screen = app.screen
        screen._show_tab("mcp")
        editor = screen.query_one(MCPSettingsEditor)
        editor.edit_mcp()
        from textual.widgets import Input, Select
        screen.query_one("#mcp-name", Input).value = "demo"
        screen.query_one("#mcp-type", Select).value = "http"
        screen.query_one("#mcp-target", Input).value = "https://example.test/mcp"
        screen.action_save_settings()
        await pilot.pause()
        assert reloads == ["reload"] and not screen._mcp_pending
        assert "已应用" in str(screen.query_one("#settings-notice", Static).render())

        async def fail_reload():
            raise ValueError("连接失败")

        monkeypatch.setattr(app._turn_runner.capabilities, "reload_mcp", fail_reload)
        editor.edit_mcp("demo")
        screen.query_one("#mcp-target", Input).value = "https://changed.test/mcp"
        screen.action_save_settings()
        await pilot.pause()
        assert screen._mcp_pending
        assert "已保存，应用失败" in str(screen.query_one("#settings-error", Static).render())
        assert app._settings_service.load().global_mcp["demo"]["url"] == "https://changed.test/mcp"
