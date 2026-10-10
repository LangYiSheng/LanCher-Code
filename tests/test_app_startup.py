from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import lancher_code.app as app_module
from lancher_code.config import ConfigBootstrapState
from lancher_code.models import AppConfig, MessageUsage, RuntimeConfig


@dataclass
class _FakeChatTUI:
    turn_runner: object
    provider_config: object
    session_controller: object
    ui_config: object
    stopped_process_count: int = 0

    async def run(self) -> int:
        return 23


class _FakeSessionController:
    visited_session_ids = ()
    session_id = None
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


    def close(self) -> None:
        self.closed = True


class _FakeToolExecutor:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _FakeTurnRunner:
    application_process_count = 0
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def configure_models(self, config, *, provider_factory) -> None:
        self.model_config = config
        self.provider_factory = provider_factory

    async def shutdown(self) -> None:
        self.closed = True


def _bootstrap_state(tmp_path: Path, *, needs_setup: bool, legacy_exists: bool = False) -> ConfigBootstrapState:
    config_path = (tmp_path / "home" / ".lancher" / "lancher.yaml").resolve()
    legacy_path = (tmp_path / "project" / "lancher.yaml").resolve()
    return ConfigBootstrapState(
        config_path=config_path,
        needs_setup=needs_setup,
        legacy_config_path=legacy_path,
        legacy_config_exists=legacy_exists,
    )


def _app_config(openai_provider_config, ui_config) -> AppConfig:
    return AppConfig(
        provider=openai_provider_config,
        ui=ui_config,
        runtime=RuntimeConfig(),
    )


@pytest.mark.asyncio
async def test_run_app_loads_global_config_without_bootstrap(monkeypatch, tmp_path, openai_provider_config, ui_config) -> None:
    state = _bootstrap_state(tmp_path, needs_setup=False)
    config = _app_config(openai_provider_config, ui_config)

    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: state)
    monkeypatch.setattr(app_module, "load_config", lambda path: config)
    monkeypatch.setattr(app_module, "create_provider", lambda provider_config, **kwargs: object())
    monkeypatch.setattr(app_module, "create_default_tool_registry", lambda: object())
    monkeypatch.setattr(app_module, "SessionController", _FakeSessionController)
    monkeypatch.setattr(app_module, "ToolExecutor", _FakeToolExecutor)
    monkeypatch.setattr(app_module, "TurnRunner", _FakeTurnRunner)
    monkeypatch.setattr(app_module, "ChatTUI", _FakeChatTUI)

    class _UnexpectedBootstrapTUI:
        def __init__(self, config_path: Path) -> None:
            raise AssertionError(f"不应该进入首次引导: {config_path}")

    monkeypatch.setattr(app_module, "ConfigBootstrapTUI", _UnexpectedBootstrapTUI)

    assert await app_module.run_app() == 23


@pytest.mark.asyncio
async def test_run_app_enters_bootstrap_when_global_config_is_missing(
    monkeypatch,
    tmp_path,
    openai_provider_config,
    ui_config,
) -> None:
    state = _bootstrap_state(tmp_path, needs_setup=True, legacy_exists=True)
    config = _app_config(openai_provider_config, ui_config)
    bootstrap_calls: list[Path] = []

    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: state)
    monkeypatch.setattr(app_module, "load_config", lambda path: config)
    monkeypatch.setattr(app_module, "create_provider", lambda provider_config, **kwargs: object())
    monkeypatch.setattr(app_module, "create_default_tool_registry", lambda: object())
    monkeypatch.setattr(app_module, "SessionController", _FakeSessionController)
    monkeypatch.setattr(app_module, "ToolExecutor", _FakeToolExecutor)
    monkeypatch.setattr(app_module, "TurnRunner", _FakeTurnRunner)
    monkeypatch.setattr(app_module, "ChatTUI", _FakeChatTUI)

    class _FakeBootstrapTUI:
        def __init__(self, config_path: Path) -> None:
            bootstrap_calls.append(config_path)

        async def run(self) -> bool:
            return True

    monkeypatch.setattr(app_module, "ConfigBootstrapTUI", _FakeBootstrapTUI)

    assert await app_module.run_app() == 23
    assert bootstrap_calls == [state.config_path]


@pytest.mark.asyncio
async def test_run_app_exits_cleanly_when_bootstrap_is_cancelled(monkeypatch, tmp_path) -> None:
    state = _bootstrap_state(tmp_path, needs_setup=True)

    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: state)

    class _FakeBootstrapTUI:
        def __init__(self, config_path: Path) -> None:
            self.config_path = config_path

        async def run(self) -> bool:
            return False

    monkeypatch.setattr(app_module, "ConfigBootstrapTUI", _FakeBootstrapTUI)

    assert await app_module.run_app() == 0


@pytest.mark.parametrize("failed_step", [None, "runner", "session", "mcp"])
async def test_exit_summary_runs_once_after_all_cleanup_and_shares_usage_tracker(
    monkeypatch, tmp_path, openai_provider_config, ui_config, failed_step,
):
    order = []
    observers = []
    summaries = []
    config = _app_config(openai_provider_config, ui_config)
    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: _bootstrap_state(tmp_path, needs_setup=False))
    monkeypatch.setattr(app_module, "load_config", lambda path: config)
    monkeypatch.setattr(app_module, "create_default_tool_registry", lambda: object())
    monkeypatch.setattr(app_module, "ToolExecutor", _FakeToolExecutor)

    def create_provider(provider_config, *, usage_observer):
        observers.append(usage_observer)
        return object()

    class Session(_FakeSessionController):
        def close(self):
            order.append("session")
            if failed_step == "session":
                raise OSError("测试保存失败")

    class Runner(_FakeTurnRunner):
        async def shutdown(self):
            order.append("runner")
            if failed_step == "runner":
                raise RuntimeError("测试任务收尾失败")

    class MCP:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            order.append("mcp")
            if failed_step == "mcp":
                raise OSError("测试连接失败")

    class TUI(_FakeChatTUI):
        stopped_process_count = 2

        async def run(self):
            # 模拟模型切换使用配置绑定的工厂，而非绕过本次启动的账本。
            self.turn_runner.provider_factory(self.provider_config)
            tracker = observers[-1]
            request_id = tracker.start_request(protocol="openai", model="gpt-test")
            tracker.update_request(request_id, MessageUsage(input_tokens=120, output_tokens=20, cached_input_tokens=90),
                                   provided_fields=frozenset({"input", "output", "cache"}))
            tracker.finish_request(request_id, status="completed")
            order.append("tui_return")
            return 0

    def summary(console, **kwargs):
        assert order == ["tui_return", "runner", "session", "mcp"]
        summaries.append(kwargs)

    monkeypatch.setattr(app_module, "create_provider", create_provider)
    monkeypatch.setattr(app_module, "SessionController", Session)
    monkeypatch.setattr(app_module, "TurnRunner", Runner)
    monkeypatch.setattr(app_module, "MCPClientManager", MCP)
    monkeypatch.setattr(app_module, "ChatTUI", TUI)
    monkeypatch.setattr(app_module, "print_exit_summary", summary)

    assert await app_module.run_app() == (1 if failed_step else 0)
    assert len(summaries) == 1 and len(observers) == 2 and observers[0] is observers[1]
    assert summaries[0]["usage"].total_tokens == 140
    assert summaries[0]["usage"].cache_hit_ratio == 0.75
    assert len(summaries[0]["cleanup_errors"]) == (1 if failed_step else 0)


async def test_uncaught_tui_error_still_closes_dependencies_without_success_farewell(
    monkeypatch, tmp_path, openai_provider_config, ui_config,
):
    order = []
    config = _app_config(openai_provider_config, ui_config)
    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: _bootstrap_state(tmp_path, needs_setup=False))
    monkeypatch.setattr(app_module, "load_config", lambda path: config)
    monkeypatch.setattr(app_module, "create_provider", lambda config, **kwargs: object())
    monkeypatch.setattr(app_module, "create_default_tool_registry", lambda: object())
    monkeypatch.setattr(app_module, "ToolExecutor", _FakeToolExecutor)

    class Session(_FakeSessionController):
        def close(self):
            order.append("session")

    class Runner(_FakeTurnRunner):
        async def shutdown(self):
            order.append("runner")

    class MCP:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            order.append("mcp")

    class TUI(_FakeChatTUI):
        async def run(self):
            raise RuntimeError("测试界面异常")

    def unexpected_summary(*args, **kwargs):
        raise AssertionError("异常退出不能打印成功告别")

    monkeypatch.setattr(app_module, "SessionController", Session)
    monkeypatch.setattr(app_module, "TurnRunner", Runner)
    monkeypatch.setattr(app_module, "MCPClientManager", MCP)
    monkeypatch.setattr(app_module, "ChatTUI", TUI)
    monkeypatch.setattr(app_module, "print_exit_summary", unexpected_summary)
    with pytest.raises(RuntimeError, match="测试界面异常"):
        await app_module.run_app()
    assert order == ["runner", "session", "mcp"]
