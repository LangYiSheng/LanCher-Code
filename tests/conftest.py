from __future__ import annotations

import io

import httpx
import pytest
from rich.console import Console

from lancher_code.models import ProviderConfig, ThinkingConfig, UIConfig


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
