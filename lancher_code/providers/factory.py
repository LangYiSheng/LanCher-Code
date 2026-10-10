from __future__ import annotations

from collections.abc import Callable

import httpx

from lancher_code.errors import ConfigError
from lancher_code.providers.models import ProviderConfig
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.usage.ledger import UsageObserver


def create_provider(
    config: ProviderConfig,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    *,
    usage_observer: UsageObserver | None = None,
) -> ChatProvider:
    if config.protocol == "openai":
        return OpenAIProvider(config=config, client_factory=client_factory, usage_observer=usage_observer)
    if config.protocol == "claude":
        return ClaudeProvider(config=config, client_factory=client_factory, usage_observer=usage_observer)
    raise ConfigError(f"不支持的 protocol: {config.protocol}")
