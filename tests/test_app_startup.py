from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import lancher_code.app as app_module
from lancher_code.config import ConfigBootstrapState
from lancher_code.models import AppConfig, RuntimeConfig


@dataclass
class _FakeChatTUI:
    turn_runner: object
    provider_config: object
    session_controller: object
    ui_config: object

    async def run(self) -> int:
        return 23


class _FakeSessionController:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _FakeToolExecutor:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _FakeTurnRunner:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


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
    monkeypatch.setattr(app_module, "create_provider", lambda provider_config: object())
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
    monkeypatch.setattr(app_module, "create_provider", lambda provider_config: object())
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
