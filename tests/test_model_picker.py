from __future__ import annotations

from copy import deepcopy

import pytest
from textual.widgets import Input, OptionList, Static

from lancher_code.config import load_config_data
from lancher_code.models import UIConfig
from lancher_code.session import SessionController
from lancher_code.slash_commands import SlashCompletionContext, create_default_slash_command_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui_views.chat import LanCherTextualApp
from lancher_code.tui_views.composer import ComposerTextArea
from lancher_code.tui_views.model_picker import ModelPickerScreen
from lancher_code.tui_views.settings import SettingsResult
from lancher_code.turn_runner import TurnRunner


def build_model_app(tmp_path):
    config = load_config_data({
        "default_model": "deepseek/chat",
        "providers": {
            "deepseek": {
                "name": "DeepSeek", "protocol": "openai",
                "base_url": "https://first.example/v1", "api_key": "first-key",
                "models": {
                    "chat": {"model_name": "deepseek-chat", "display_name": "日常编程"},
                    "reason": {"model_name": "deepseek-reasoner"},
                },
            },
            "other": {
                "name": "备用服务", "protocol": "claude",
                "base_url": "https://second.example/v1", "api_key": "second-key",
                "models": {"chat": {"model_name": "chat-api", "display_name": "日常编程"}},
            },
        },
    })
    session = SessionController(config.provider, cwd=tmp_path)
    registry = ToolRegistry()
    executor = ToolExecutor(registry, cwd=tmp_path, timeout_seconds=1)
    constructed = []

    def factory(resolved):
        constructed.append(resolved)
        return object()

    runner = TurnRunner(object(), session, registry, executor)
    runner.configure_models(config, provider_factory=factory)
    app = LanCherTextualApp(runner, config.provider, session, UIConfig(), tool_registry=registry)
    return app, runner, session, config, constructed


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_model_picker_keyboard_search_and_cancel(tmp_path, size):
    app, runner, session, config, constructed = build_model_app(tmp_path)
    async with app.run_test(size=size) as pilot:
        await app._execute_slash_command("model", "")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        assert screen.query_one("#model-options", OptionList).region.height >= 2
        assert screen.query_one("#model-picker").region.right <= size[0]
        assert screen.query_one("#model-picker-help").region.bottom <= size[1]
        assert len(screen._visible_refs) == 3
        screen.query_one("#model-search", Input).value = "备用 日常"
        await pilot.pause()
        assert screen._visible_refs == ["other/chat"]
        await pilot.press("enter")
        await pilot.pause()
        assert runner.model_ref == "other/chat"
        assert app._status_left_text() == "日常编程"
        assert config.default_model == "deepseek/chat"
        assert session.transcript == []
        assert len(constructed) == 1
        assert constructed[0].model == "chat-api"

        await app._execute_slash_command("model", "")
        await pilot.pause()
        await pilot.press("up", "escape")
        await pilot.pause()
        assert runner.model_ref == "other/chat"
        assert isinstance(app.focused, ComposerTextArea)


@pytest.mark.asyncio
async def test_model_picker_empty_search_then_direct_selection(tmp_path):
    app, runner, session, _, constructed = build_model_app(tmp_path)
    async with app.run_test() as pilot:
        await app._execute_slash_command("model", "")
        await pilot.pause()
        app.screen.query_one("#model-search", Input).value = "not-a-model"
        await pilot.pause()
        assert app.screen.query_one("#model-picker-empty", Static).display
        await pilot.press("enter")
        assert isinstance(app.screen, ModelPickerScreen)
        await pilot.press("escape")
        await pilot.pause()
        await app._execute_slash_command("model", "deepseek/reason")
        assert runner.model_ref == "deepseek/reason"
        assert app._status_left_text() == "deepseek-reasoner (DeepSeek)"
        await app._execute_slash_command("model", "missing/model")
        assert runner.model_ref == "deepseek/reason"
        assert len(constructed) == 1
        assert session.transcript == []


def test_model_completion_preserves_stable_reference_and_description():
    registry = create_default_slash_command_registry()
    candidates = registry.complete(SlashCompletionContext(
        text="/model deep", mode="plan",
        model_choices=(("deepseek/chat", "日常编程"), ("other/chat", "日常编程")),
        active_model_ref="deepseek/chat",
    ))
    assert len(candidates) == 1
    assert candidates[0].apply("/model deep") == "/model deepseek/chat"
    assert "日常编程" in candidates[0].description
    assert "当前" in candidates[0].description


@pytest.mark.asyncio
async def test_chat_applies_settings_without_changing_active_model_for_new_default(tmp_path):
    app, runner, _, config, constructed = build_model_app(tmp_path)
    async with app.run_test() as pilot:
        changed = deepcopy(config)
        changed.default_model = "other/chat"
        app._handle_settings_result(SettingsResult(saved=True, config=changed))
        await pilot.pause()
        assert runner.model_ref == "deepseek/chat"
        assert runner.model_config.default_model == "other/chat"
        assert not constructed

        changed.providers["deepseek"].models["chat"].display_name = "日常 [fast]"
        changed.providers["deepseek"].api_key = "edited-key"
        app._handle_settings_result(SettingsResult(saved=True, config=changed))
        await pilot.pause()
        assert constructed[-1].api_key == "edited-key"
        assert "[fast]" in str(app.query_one("#status-left", Static).render())

        del changed.providers["deepseek"]
        app._handle_settings_result(SettingsResult(saved=True, config=changed))
        await pilot.pause()
        assert runner.model_ref == "other/chat"
        assert app._status_left_text() == "日常编程"


@pytest.mark.asyncio
async def test_chat_resume_restores_saved_model_then_missing_model_falls_back(tmp_path):
    app, runner, session, config, _ = build_model_app(tmp_path)
    runner.switch_model("other/chat")
    session.save_session("second")
    session.save_session("working")
    async with app.run_test() as pilot:
        runner.switch_model("deepseek/chat")
        await app._execute_slash_command("session", "resume second --force")
        await pilot.pause()
        assert runner.model_ref == "other/chat"
        session.save_session("working-again")
        changed = deepcopy(config)
        del changed.providers["other"]
        runner.reload_models(changed)
        await app._execute_slash_command("session", "resume second --force")
        assert runner.model_ref == "deepseek/chat"
        assert "不存在" in runner.model_notice
