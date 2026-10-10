"""真实聊天应用、设置页面与智能体核心的系统设置联动。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from textual.widgets import Checkbox, Static

from lancher_code.agent.runner import TurnRunner
from lancher_code.agent.skills import SkillsService
from lancher_code.config.loader import load_config
from lancher_code.config.models import AppConfig
from lancher_code.config.settings import SettingsService
from lancher_code.config.writer import write_config
from lancher_code.contracts.messages import ChatRequest, ContentBlock, StreamEvent
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.providers.catalog import resolve_model
from lancher_code.providers.models import ModelDefinition, ProviderDefinition
from lancher_code.sessions.controller import SessionController
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.tui.app import LanCherTextualApp
from lancher_code.tui.composer import ComposerSubmitted, ComposerTextArea
from lancher_code.tui.settings.screen import SettingsScreen


class RecordingProvider:
    """记录实际请求，门闩用于让设置保存遇到真实正在运行的回合。"""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def stream_chat(self, request: ChatRequest):
        self.requests.append(request)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        text = "已完成当前任务"
        yield StreamEvent(kind="text_delta", text=text)
        yield StreamEvent(kind="message_end", response_complete=True,
                          assistant_blocks=[ContentBlock.text_block(text)])


def _build_app(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    user_root = tmp_path / "home"
    config_path = user_root / ".lancher" / "lancher.yaml"
    config = AppConfig(providers={"test": ProviderDefinition(
        name="测试供应商", protocol="openai", base_url="https://example.test/v1",
        api_key="test-key", models={"chat": ModelDefinition(model_name="test-model")},
    )}, default_model="test/chat")
    write_config(config_path, config)
    permissions = PermissionStorage(
        project_rules_path=project / ".lancher" / "permissions.yaml",
        user_rules_path=config_path.parent / "permissions.yaml",
    )
    service = SettingsService(config_path=config_path,
                              global_mcp_path=config_path.parent / "mcp.yaml",
                              project_mcp_path=project / ".lancher" / "mcp.yaml",
                              permission_storage=permissions)
    provider_config = resolve_model(config.providers, config.default_model)
    session = SessionController(provider_config, cwd=project, permission_storage=permissions)
    registry = ToolRegistry()
    executor = ToolExecutor(registry, cwd=project)
    provider = RecordingProvider()
    runner = TurnRunner(provider, session, registry, executor,
                        skills_service=SkillsService(project, user_root=user_root))
    runner.configure_models(config, provider_factory=lambda resolved: provider)
    app = LanCherTextualApp(runner, provider_config, session, config.ui, settings_service=service)
    return app, runner, session, provider, service


async def _until(pilot, predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause(0.01)


async def _submit(app, pilot, text: str) -> None:
    previous = len(app._turn_runner._models.provider.requests)
    composer = app.query_one(ComposerTextArea)
    await app.handle_input_submitted(ComposerSubmitted(composer, text))
    await _until(pilot, lambda: len(app._turn_runner._models.provider.requests) > previous
                 and not app._is_streaming and not app._turn_runner.has_active_turn)


async def _open_system(app, pilot) -> SettingsScreen:
    # 使用真实 slash 路由，回调与 SettingsResult 的处理均由聊天应用接线。
    await app._execute_slash_command("settings", "open")
    await pilot.pause()
    assert isinstance(app.screen, SettingsScreen)
    await pilot.click("#tab-runtime")
    return app.screen


async def _close_settings(screen, pilot) -> None:
    screen.action_close_settings()
    await pilot.pause()
    screen.action_close_settings()
    await pilot.pause()


def _history_snapshot(session):
    return (deepcopy(session.context_state), deepcopy(session.transcript),
            session.paths.events.read_bytes())


@pytest.mark.asyncio
async def test_app_runtime_save_calls_core_and_hud_only_previews_until_next_request(tmp_path, monkeypatch):
    app, runner, session, provider, service = _build_app(tmp_path)
    calls = []
    validate, apply = runner.validate_runtime_settings, runner.apply_runtime_settings

    def validate_saved(runtime):
        persisted = load_config(service.config_path).runtime.experimental_mcp_tool_append
        calls.append(("validate", persisted))
        validate(runtime)

    def apply_saved(runtime):
        assert load_config(service.config_path).runtime.experimental_mcp_tool_append
        calls.append(("apply", True))
        return apply(runtime)

    # 保留实际核心实现；第一次预验证在写盘前，应用时再次验证核心空闲。
    monkeypatch.setattr(runner, "apply_runtime_settings", apply_saved)
    async with app.run_test(size=(100, 40)) as pilot:
        await _submit(app, pilot, "已有任务")
        baseline = _history_snapshot(session)
        epoch = session.context_state.prefix_state["epoch"]
        phase, policy = session.work_phase, session.permission_policy
        monkeypatch.setattr(runner, "validate_runtime_settings", validate_saved)
        screen = await _open_system(app, pilot)
        screen.query_one("#runtime-mcp-tool-append", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert calls == [("validate", False), ("apply", True), ("validate", True)]
        assert not screen._system_pending
        assert runner.model_config.runtime.experimental_mcp_tool_append
        assert session._prompt_epoch_reset
        assert (session.work_phase, session.permission_policy) == (phase, policy)
        assert _history_snapshot(session) == baseline
        assert "下一次请求重建" in str(screen.query_one("#settings-notice", Static).render())

        # 系统应用和 HUD 刷新已触发预览，但不发布新的 epoch、host_update 或持久化事件。
        for _ in range(3):
            app._refresh_context_usage()
        assert _history_snapshot(session) == baseline
        assert len(provider.requests) == 1
        assert session._prompt_epoch_reset
        await _close_settings(screen, pilot)
        assert len(app.screen_stack) == 1
        assert _history_snapshot(session) == baseline
        assert session._prompt_epoch_reset

        await _submit(app, pilot, "应用实验设置后的任务")
        assert len(provider.requests) == 2
        assert not provider.requests[0].experimental_mcp_tool_append
        assert provider.requests[1].experimental_mcp_tool_append
        assert session.context_state.prefix_state["epoch"] != epoch
        assert not session._prompt_epoch_reset
        assert session.paths.events.read_bytes() != baseline[2]


@pytest.mark.asyncio
async def test_app_runtime_busy_validation_keeps_disk_and_current_runtime(tmp_path):
    app, runner, session, provider, service = _build_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await _submit(app, pilot, "已有任务")
        screen = await _open_system(app, pilot)
        screen.query_one("#runtime-mcp-tool-append", Checkbox).value = True
        original = service.config_path.read_bytes()
        provider.started.clear()
        provider.release = asyncio.Event()

        async def consume():
            return [event async for event in runner.run_user_turn("设置页打开后开始的任务")]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(provider.started.wait(), timeout=5)
            assert runner.has_active_turn
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert service.config_path.read_bytes() == original
            assert not runner.model_config.runtime.experimental_mcp_tool_append
            assert not session._prompt_experiments
            assert not screen._saved and not screen._system_pending
            assert screen.query_one("#runtime-mcp-tool-append", Checkbox).value
            assert "模型正在响应" in str(screen.query_one("#settings-error", Static).render())
        finally:
            provider.release.set()
            await asyncio.wait_for(task, timeout=5)
        # 返回编辑前的值即可关闭，禁止把未保存的草稿应用到核心。
        screen.query_one("#runtime-mcp-tool-append", Checkbox).value = False
        await _close_settings(screen, pilot)
        assert len(app.screen_stack) == 1
        assert not runner.model_config.runtime.experimental_mcp_tool_append
        assert service.config_path.read_bytes() == original


@pytest.mark.asyncio
async def test_app_failed_runtime_apply_stays_pending_after_close_and_model_reload(tmp_path, monkeypatch):
    app, runner, session, provider, service = _build_app(tmp_path)
    apply_calls = []
    model_reloads = []
    apply, reload_models = runner.apply_runtime_settings, runner.reload_models

    def fail_apply(runtime):
        apply_calls.append(runtime.experimental_mcp_tool_append)
        raise RuntimeError("模拟核心应用失败")

    def reload(config):
        model_reloads.append(config.runtime.experimental_mcp_tool_append)
        return reload_models(config)

    monkeypatch.setattr(runner, "apply_runtime_settings", fail_apply)
    monkeypatch.setattr(runner, "reload_models", reload)
    async with app.run_test(size=(100, 40)) as pilot:
        await _submit(app, pilot, "已有任务")
        baseline = _history_snapshot(session)
        epoch = session.context_state.prefix_state["epoch"]
        screen = await _open_system(app, pilot)
        screen.query_one("#runtime-mcp-tool-append", Checkbox).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert apply_calls == [True]
        assert load_config(service.config_path).runtime.experimental_mcp_tool_append
        assert screen._saved and screen._system_pending
        assert "已保存" in str(screen.query_one("#settings-error", Static).render())
        assert "应用失败" in str(screen.query_one("#settings-error", Static).render())
        assert not runner.model_config.runtime.experimental_mcp_tool_append
        assert not session._prompt_experiments and not session._prompt_epoch_reset
        assert _history_snapshot(session) == baseline

        await _close_settings(screen, pilot)
        assert len(app.screen_stack) == 1
        assert model_reloads == [True]
        assert apply_calls == [True]
        assert "系统设置待应用" in app._status_hint
        # SettingsResult.config 携带已保存的 True，模型热重载仍保留当前运行时 False。
        assert not runner.model_config.runtime.experimental_mcp_tool_append
        assert not session._prompt_experiments and not session._prompt_epoch_reset
        assert _history_snapshot(session) == baseline
        await _submit(app, pilot, "继续使用当前设置")
        assert not provider.requests[-1].experimental_mcp_tool_append
        assert session.context_state.prefix_state["epoch"] == epoch

        # 显式重试系统保存才应用，关闭页面与重载模型目录均不能旁路应用。
        monkeypatch.setattr(runner, "apply_runtime_settings", apply)
        screen = await _open_system(app, pilot)
        assert screen.query_one("#runtime-mcp-tool-append", Checkbox).value
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not screen._system_pending
        assert runner.model_config.runtime.experimental_mcp_tool_append
        assert session._prompt_experiments and session._prompt_epoch_reset
        await _close_settings(screen, pilot)
