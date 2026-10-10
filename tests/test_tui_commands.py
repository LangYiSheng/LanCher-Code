from __future__ import annotations

import pytest
from textual.widgets import Static

from lancher_code.models import StreamEvent, UIConfig
from lancher_code.session import SessionController
from lancher_code.sessions.repository import SessionRepositoryError
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
async def test_first_message_rename_new_and_resume_are_isolated(openai_provider_config, tmp_path):
    response = [StreamEvent(kind="text_delta", text="收到"), StreamEvent(kind="message_end")]
    app, session = _build_app(FakeProvider([response, response]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        assert session.session_id is None
        composer = await type_command(app, pilot, "第一条消息")
        await pilot.press("enter")
        await pilot.pause()
        original_id = session.session_id
        original_workspace = session.paths.workspace
        assert original_id and session.paths.events.is_file()
        await type_command(app, pilot, f"/session rename {original_id} 命令改版  新标题")
        await pilot.press("enter")
        assert session.session_title == "命令改版  新标题"
        assert session.session_id == original_id
        await type_command(app, pilot, "/session new")
        await pilot.press("tab", "enter")
        assert session.session_id is None and session.paths is None
        assert not session.state.messages and not app._chat_started
        await type_command(app, pilot, "另一个任务")
        await pilot.press("enter")
        await pilot.pause()
        assert session.session_id != original_id and session.paths.workspace != original_workspace
        await type_command(app, pilot, f"/session resume {original_id}")
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert session.session_id == original_id
        assert session.session_title == "命令改版  新标题"
        assert session.state.messages[0].content == "第一条消息"
        assert composer.text == ""
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["archive", "remove"])
async def test_destructive_action_cancel_then_confirm_preserves_new_draft(openai_provider_config, tmp_path, action):
    saved = SessionController(openai_provider_config, cwd=tmp_path)
    saved.create_user_message("旧内容")
    saved_id = saved.session_id
    events_path = saved.paths.events
    original = events_path.read_bytes()
    saved.close()
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, f"/session {action} {saved_id}")
        await pilot.press("tab", "enter")
        assert isinstance(app.screen, CommandConfirmationScreen)
        await pilot.press("escape")
        assert composer.text == f"/session {action} {saved_id}"
        assert events_path.read_bytes() == original
        await pilot.press("enter")
        await pilot.pause()
        composer.text = "新草稿"
        await pilot.click("#command-confirm")
        await pilot.pause()
        assert composer.text == "新草稿"
        if action == "remove":
            assert not events_path.parent.exists()
        else:
            assert events_path.is_file()
            assert next(item for item in session.list_sessions() if item.session_id == saved_id).archived
        assert session.session_id is None


@pytest.mark.asyncio
async def test_stale_confirmation_cannot_change_new_session(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    other = SessionController(openai_provider_config, cwd=tmp_path)
    other.create_user_message("保留此会话")
    other_id = other.session_id
    other.close()
    session.create_user_message("当前会话")
    async with app.run_test() as pilot:
        await type_command(app, pilot, f"/session remove {other_id}")
        await pilot.press("tab", "enter")
        app._turn_runner.new_session()
        await pilot.click("#command-confirm")
        await pilot.pause()
        assert other_id in [item.session_id for item in session.list_sessions()]
        assert session.session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_draft", ["", "后来编辑的新草稿"])
async def test_failed_session_creation_keeps_draft_for_retry(openai_provider_config, tmp_path, monkeypatch, replacement_draft):
    provider = FakeProvider([[StreamEvent(kind="text_delta", text="已收到"), StreamEvent(kind="message_end")]])
    app, session = _build_app(provider, openai_provider_config, UIConfig(), tmp_path)
    create = session._sessions.repository.create

    def fail(*_args):
        if replacement_draft:
            app.query_one(ComposerTextArea).text = replacement_draft
        raise SessionRepositoryError("磁盘不可写")

    monkeypatch.setattr(session._sessions.repository, "create", fail)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "需要保留的第一条消息")
        await pilot.press("enter")
        for _ in range(60):
            if not app._is_streaming:
                break
            await pilot.pause(0.05)
        assert not app._is_streaming and not provider.requests
        assert session.session_id is None and not app._chat_started
        expected_draft = replacement_draft or "需要保留的第一条消息"
        assert composer.text == expected_draft
        monkeypatch.setattr(session._sessions.repository, "create", create)
        await pilot.press("enter")
        for _ in range(60):
            if not app._is_streaming:
                break
            await pilot.pause(0.05)
        assert session.session_id and len(provider.requests) == 1
        assert session.state.messages[0].content == expected_draft
        assert composer.text == ""


@pytest.mark.asyncio
async def test_corrupt_session_query_stays_usable_and_remove_confirms_uuid(openai_provider_config, tmp_path):
    saved = SessionController(openai_provider_config, cwd=tmp_path)
    saved.create_user_message("损坏记录")
    saved_id = saved.session_id
    events_path = saved.paths.events
    saved.close()
    events_path.write_text("损坏的完整记录\n", encoding="utf-8")
    app, _ = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        await type_command(app, pilot, "/session ")
        assert "会话列表不可用" in str(app.query_one(CommandHintBar).render())
        await type_command(app, pilot, f"/session remove {saved_id}")
        await pilot.press("enter")
        assert isinstance(app.screen, CommandConfirmationScreen)
        assert saved_id in app.screen.description
        await pilot.click("#command-confirm")
        await pilot.pause()
        assert not events_path.parent.exists()


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
        for _ in range(10):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_session_title_menu_keeps_identity_visible_and_fills_uuid(openai_provider_config, tmp_path, size):
    title = "HTTP 服务测试 · 同名会话恢复"
    saved = SessionController(openai_provider_config, cwd=tmp_path)
    saved.create_user_message("原对话")
    original_id = saved.session_id
    saved.rename_session(original_id, title)
    saved.close()
    second = SessionController(openai_provider_config, cwd=tmp_path)
    second.create_user_message("另一个同名对话")
    second_id = second.session_id
    second.rename_session(second_id, title)
    second.close()
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    session.archive_session(second_id)
    async with app.run_test(size=size) as pilot:
        composer = await type_command(app, pilot, "/session resume 服务 HTTP")
        items = list(app.query(SlashCompletionMenuItem))
        assert len(items) == 2
        assert [item.candidate.value for item in items] == [second_id, original_id]
        active = next(item for item in items if item._active)
        assert active.candidate.display == title
        assert active.render().plain.splitlines()[0].startswith("› HTTP")
        assert second_id[:8] in active.render().plain and "已归档" in active.render().plain
        hint = app.query_one(CommandHintBar)
        if size[1] >= 24:
            assert title in str(hint.render())
        else:
            assert "HTTP 服务测试" in str(hint.render())
        assert title in active.candidate.detail and second_id in str(hint.render())
        assert composer.region.bottom <= size[1] and composer.region.height >= 1
        assert hint.region.right <= size[0]
        assert hint.region.bottom <= app.query_one("#composer-actions").region.y
        assert hint.region.height >= (3 if size[0] == 32 else 2)
        await pilot.press("down")
        assert original_id in str(hint.render())
        await pilot.press("tab")
        assert composer.text == f"/session resume {original_id}"
        await pilot.press("enter")
        await pilot.pause()
        assert session.session_id == original_id and session.state.messages[0].content == "原对话"


@pytest.mark.asyncio
async def test_list_and_new_do_not_create_session_and_active_destructive_commands_are_refused(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test() as pilot:
        composer = await type_command(app, pilot, "/session list")
        await pilot.press("tab", "enter")
        assert session.session_id is None and not session.list_sessions()
        await pilot.press("escape")
        await type_command(app, pilot, "/session new")
        await pilot.press("tab", "enter")
        assert session.session_id is None and not session.list_sessions()
        session.create_user_message("当前任务")
        for action in ("archive", "remove"):
            with pytest.raises(SessionRepositoryError, match="先运行 /session new"):
                await app._execute_session_command(f"{action} {session.session_id}")
        assert len(app.screen_stack) == 1
