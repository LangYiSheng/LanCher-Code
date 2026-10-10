from __future__ import annotations

import io

import httpx
import pytest
from rich.console import Console

from lancher_code.providers.models import ProviderConfig, ThinkingConfig
from lancher_code.config.models import UIConfig


@pytest.fixture
def openai_provider_config() -> ProviderConfig:
    return ProviderConfig(
        protocol="openai",
        model="gpt-test",
        base_url="https://example.com/v1",
        api_key="test-key",
        timeout_seconds=30.0,
    )


@pytest.fixture
def claude_provider_config() -> ProviderConfig:
    return ProviderConfig(
        protocol="claude",
        model="claude-test",
        base_url="https://example.com",
        api_key="test-key",
        timeout_seconds=30.0,
        thinking=ThinkingConfig(enabled=True, budget_tokens=512),
    )


@pytest.fixture
def ui_config() -> UIConfig:
    return UIConfig(show_timestamps=False, show_thinking_status=True)


@pytest.fixture
def console_and_buffer() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, color_system=None, width=120)
    return console, buffer


def mock_client_factory(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, timeout=30.0)


@pytest.fixture(autouse=True)
def isolated_project_directory(tmp_path, monkeypatch):
    """每个测试使用自己的项目，防止自动会话落盘污染真实工作目录。"""
    monkeypatch.chdir(tmp_path)


def app_config_for(provider: ProviderConfig, **overrides):
    """业务测试显式构造当前供应商目录，不使用生产兼容入口。"""
    from lancher_code.config.models import AppConfig
    from lancher_code.providers.models import ProviderDefinition, ModelDefinition
    return AppConfig(providers={"test": ProviderDefinition(
        name="测试", protocol=provider.protocol, base_url=provider.base_url,
        api_key=provider.api_key, timeout_seconds=provider.timeout_seconds,
        models={"default": ModelDefinition(model_name=provider.model,
            context_window=provider.context_window, thinking=provider.thinking)},
    )}, default_model="test/default", **overrides)
