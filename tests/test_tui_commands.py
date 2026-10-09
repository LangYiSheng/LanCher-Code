from __future__ import annotations

import pytest
from textual.widgets import Static

from lancher_code.models import UIConfig
from lancher_code.session import SessionController
from lancher_code.session_store import SessionStoreError
from lancher_code.tui_views.command_actions import CommandConfirmationScreen
from lancher_code.tui_views.composer import CommandHintBar, ComposerSubmitted, ComposerTextArea, SlashCompletionMenu, SlashCompletionMenuItem
from lancher_code.tui_views.theme import apply_theme
from test_model_picker import build_model_app
from test_settings import _service
from test_tui_flow import FakeProvider, _build_app
from test_tui_native import GatedProvider


async def type_command(app, pilot, text):
    composer = app.query_one(ComposerTextArea)
    composer.text = text
    composer.cursor_location = composer.document.end
    composer.focus()
    await pilot.pause()
    return composer


@pytest.mark.asyncio
async def test_save_free_name_tab_optional_enter_skips_force(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/session save ")
        assert "输入一个会话名" in str(app.query_one(CommandHintBar).render())
        await pilot.press("enter")
        assert composer.text == "/session save "
        assert session.active_session_name is None
        await type_command(app, pilot, "/session save 命令改版")
        await pilot.press("tab")
        assert composer.text == "/session save 命令改版 "
        assert app.query_one(SlashCompletionMenu).display
        assert not composer.slash_enter_accepts
        assert "Enter 执行" in str(app.query_one("#composer-help", Static).render())
        await pilot.press("enter")
        assert session.active_session_name == "命令改版"
        assert composer.text == ""
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_force_cancel_then_confirm_once_and_preserve_new_draft(openai_provider_config, tmp_path):
    saved = SessionController(openai_provider_config, cwd=tmp_path)
    saved.create_user_message("旧内容")
    saved.save_session("saved")
    original = (tmp_path / '.lancher/session/saved.jsonl').read_bytes()
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.create_user_message("新内容")
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/session save saved --force")
        await pilot.press("enter")
        assert isinstance(app.screen, CommandConfirmationScreen)
        await pilot.press("escape")
        assert composer.text == "/session save saved --force"
        assert (tmp_path / '.lancher/session/saved.jsonl').read_bytes() == original
        await pilot.press("enter")
        await pilot.pause()
        composer.text = "新草稿"
        await pilot.click("#command-confirm")
        await pilot.pause()
        assert composer.text == "新草稿"
        assert session.active_session_name == "saved"
        restored = SessionController(openai_provider_config, cwd=tmp_path)
        restored.resume_session("saved")
        assert restored.state.messages[0].content == "新内容"


@pytest.mark.asyncio
async def test_stale_confirmation_cannot_change_new_session(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    other = SessionController(openai_provider_config, cwd=tmp_path)
    other.save_session("other")
    async with app.run_test() as pilot:
        await type_command(app, pilot, "/session save overwritten --force")
        await pilot.press("enter")
        session.resume_session("other", force=True)
        await pilot.click("#command-confirm")
        await pilot.pause()
        assert not (tmp_path / '.lancher/session/overwritten.jsonl').exists()
        assert session.active_session_name == "other"


@pytest.mark.asyncio
async def test_permissions_and_models_execute_in_command_menu(tmp_path):
    app, runner, session, config, _ = build_model_app(tmp_path)
    async with app.run_test() as pilot:
        runner.set_phase("discuss")
        composer = await type_command(app, pilot, "/permissions b")
        await pilot.press("tab")
        assert composer.text == "/permissions bypass"
        assert session.permission_policy == "default"
        await pilot.press("enter")
        assert session.permission_policy == "bypass" and session.work_phase == "discuss"
        await type_command(app, pilot, "/model other")
        await pilot.press("tab", "enter")
        assert runner.model_ref == "other/chat"
        assert config.default_model == "deepseek/chat"
        assert len(app.screen_stack) == 1
        await type_command(app, pilot, "/model missing/model")
        await pilot.press("enter")
        assert composer.text == "/model missing/model"
        assert runner.model_ref == "other/chat"


@pytest.mark.asyncio
async def test_settings_commands_save_separate_domains_and_failed_save_keeps_draft(tmp_path, monkeypatch):
    app, runner, _, config, _ = build_model_app(tmp_path)
    service = _service(tmp_path)
    service.save_models(config)
    app._settings_service = service
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/settings theme li")
        await pilot.press("tab", "enter")
        assert app.theme == "lancher-light" and service.load().config.ui.theme == "light"
        await app._execute_slash_command("settings", "thinking off")
        await app._execute_slash_command("settings", "busy-enter draft")
        await app._execute_slash_command("settings", "default-model other/chat")
        assert not app._ui_config.show_thinking_status and app._ui_config.busy_enter_action == "draft"
        assert runner.model_ref == "deepseek/chat" and runner.model_config.default_model == "other/chat"
        assert not service.project_mcp_path.exists() and not service.global_mcp_path.exists()
        assert not service.permission_storage.project_rules_path.exists()
        def fail(_ui):
            raise OSError("保存失败")
        monkeypatch.setattr(service, "save_ui", fail)
        await type_command(app, pilot, "/settings theme dark")
        composer.remember_accepted_slash_command(composer.text)
        await app.handle_input_submitted(ComposerSubmitted(composer, composer.text))
        assert composer.text == "/settings theme dark" and app.theme == "lancher-light"


@pytest.mark.asyncio
async def test_unknown_command_and_busy_command_are_not_sent(openai_provider_config, tmp_path):
    provider = GatedProvider()
    app, _ = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/mode plan")
        await pilot.press("enter")
        assert not provider.requests and composer.text == "/mode plan"
        app.action_toggle_details()
        await type_command(app, pilot, "开始任务")
        await pilot.press("enter")
        await provider.started.wait()
        assert not app._details_open
        app.action_toggle_details()
        await type_command(app, pilot, "/permissions bypass")
        await pilot.press("enter")
        assert composer.text == "/permissions bypass" and app._details_open
        assert "草稿已保留" in str(app.query_one(CommandHintBar).render())
        await type_command(app, pilot, "下一轮")
        await pilot.press("enter")
        assert not app._details_open and composer.text == ""
        provider.release.set()
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_menu_keyboard_and_layout_across_sizes(tmp_path, theme, size):
    app, _, _, _, _ = build_model_app(tmp_path)
    app._ui_config.theme = theme
    apply_theme(app, theme)
    async with app.run_test(size=size) as pilot:
        assert app.theme == f"lancher-{theme}"
        composer = await type_command(app, pilot, "/")
        assert "Enter 填入" in str(app.query_one("#composer-help", Static).render())
        for _ in range(9):
            await pilot.press("down")
        menu = app.query_one(SlashCompletionMenu)
        item = [i for i in app.query(SlashCompletionMenuItem) if i._active][0]
        assert item.candidate.display == "exit"
        assert menu.region.contains(item.region.x, item.region.y)
        assert composer.region.bottom <= size[1] and composer.region.height >= 1
        assert app.query_one("#composer").region.right <= size[0]
        await pilot.press("escape")
        await app._refresh_command_ui()
        assert not menu.display and composer.text == "/"


def test_save_failure_restores_binding_and_force_requires_explicit_opt_in(openai_provider_config, tmp_path, monkeypatch):
    session = SessionController(openai_provider_config, cwd=tmp_path)
    session.save_session("first")
    session.save_session("second")
    with pytest.raises(SessionStoreError):
        session.save_session("first")
    def fail(*_args):
        raise SessionStoreError("磁盘不可写")
    monkeypatch.setattr(session._session_store, "save", fail)
    with pytest.raises(SessionStoreError):
        session.save_session("first", force=True)
    assert session.active_session_name == "second"
