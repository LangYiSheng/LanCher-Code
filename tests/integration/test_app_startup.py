from __future__ import annotations

from pathlib import Path

import pytest
from conftest import app_config_for

import lancher_code.app as app_module
from lancher_code.config.bootstrap import ConfigBootstrapState
from lancher_code.config.models import AgentConfig
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.sdk import AgentShutdownError
from lancher_code.usage.ledger import RunUsageTracker
from lancher_code.usage.models import MessageUsage


class FakeAgent:
    application_process_count = 0
    visited_session_ids = ()
    session_id = None

    def __init__(self, order, error=None):
        self.order, self.error = order, error
        self.permission_storage = PermissionStorage()
        self.tracker = RunUsageTracker()
        self.started = False
        self.close_count = 0

    def start_capabilities(self):
        self.started = True

    async def close(self):
        self.close_count += 1
        self.order.append("agent_closed")
        if self.error:
            raise AgentShutdownError((self.error,))

    def run_usage(self):
        return self.tracker.snapshot()


def bootstrap(tmp_path, needs_setup=False):
    return ConfigBootstrapState(config_path=tmp_path / "config.yaml", needs_setup=needs_setup)


def install_app(monkeypatch, tmp_path, config, agent, *, needs_setup=False):
    state = bootstrap(tmp_path, needs_setup)
    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: state)
    monkeypatch.setattr(app_module, "load_config", lambda path: config)
    def create_agent(core_config, *, cwd):
        assert isinstance(core_config, AgentConfig) and not hasattr(core_config, "ui")
        assert core_config.providers == config.providers
        assert cwd == tmp_path
        return agent
    monkeypatch.setattr(app_module, "create_agent", create_agent)
    return state


@pytest.mark.parametrize("needs_setup", [False, True])
async def test_tui_startup_consumes_sdk_and_preserves_bootstrap(monkeypatch, tmp_path, openai_provider_config, needs_setup):
    order, calls = [], []
    agent = FakeAgent(order)
    state = install_app(monkeypatch, tmp_path, app_config_for(openai_provider_config), agent, needs_setup=needs_setup)
    class Setup:
        def __init__(self, path):
            calls.append(path)
        async def run(self):
            return True
    class TUI:
        stopped_process_count = 0
        def __init__(self, *, agent: FakeAgent, ui_config, settings_service):
            self.agent = agent
        async def run(self):
            assert self.agent.started
            order.append("tui_return")
            return 23
    monkeypatch.setattr(app_module, "ConfigBootstrapTUI", Setup)
    monkeypatch.setattr(app_module, "ChatTUI", TUI)
    assert await app_module.run_app() == 23
    assert calls == ([state.config_path] if needs_setup else [])
    assert order == ["tui_return", "agent_closed"] and agent.close_count == 1


async def test_cancelled_bootstrap_does_not_create_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "resolve_config_bootstrap_state", lambda: bootstrap(tmp_path, True))
    class Setup:
        def __init__(self, path):
            pass
        async def run(self):
            return False
    def unexpected(*args, **kwargs):
        raise AssertionError("取消引导后不应装配 Agent")
    monkeypatch.setattr(app_module, "ConfigBootstrapTUI", Setup)
    monkeypatch.setattr(app_module, "create_agent", unexpected)
    assert await app_module.run_app() == 0


@pytest.mark.parametrize("error", [None, "会话保存失败"])
async def test_farewell_runs_once_after_sdk_close_and_preserves_usage(monkeypatch, tmp_path, openai_provider_config, error):
    order, summaries = [], []
    agent = FakeAgent(order, error)
    install_app(monkeypatch, tmp_path, app_config_for(openai_provider_config), agent)
    class TUI:
        stopped_process_count = 2
        def __init__(self, **kwargs):
            pass
        async def run(self):
            identity = agent.tracker.start_request(protocol="openai", model="gpt-test")
            agent.tracker.update_request(identity, MessageUsage(input_tokens=120, output_tokens=20, cached_input_tokens=90),
                provided_fields=frozenset({"input", "output", "cache"}))
            agent.tracker.finish_request(identity, status="completed")
            order.append("tui_return")
            return 0
    def summary(console, **kwargs):
        assert order == ["tui_return", "agent_closed"]
        summaries.append(kwargs)
    monkeypatch.setattr(app_module, "ChatTUI", TUI)
    monkeypatch.setattr(app_module, "print_exit_summary", summary)
    assert await app_module.run_app() == (1 if error else 0)
    assert agent.close_count == 1 and len(summaries) == 1
    assert summaries[0]["usage"].total_tokens == 140
    assert summaries[0]["usage"].cache_hit_ratio == 0.75
    assert summaries[0]["cleanup_errors"] == ((error,) if error else ())
    assert summaries[0]["stopped_processes"] == 2


@pytest.mark.parametrize("construction_error", [False, True])
async def test_tui_error_still_closes_sdk_without_success_farewell(monkeypatch, tmp_path, openai_provider_config, construction_error):
    order = []
    agent = FakeAgent(order)
    install_app(monkeypatch, tmp_path, app_config_for(openai_provider_config), agent)
    class TUI:
        stopped_process_count = 0
        def __init__(self, **kwargs):
            if construction_error:
                raise RuntimeError("测试界面异常")
        async def run(self):
            raise RuntimeError("测试界面异常")
    def unexpected(*args, **kwargs):
        raise AssertionError("异常退出不能打印成功告别")
    monkeypatch.setattr(app_module, "ChatTUI", TUI)
    monkeypatch.setattr(app_module, "print_exit_summary", unexpected)
    with pytest.raises(RuntimeError, match="测试界面异常"):
        await app_module.run_app()
    assert order == ["agent_closed"] and agent.close_count == 1
