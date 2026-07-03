from __future__ import annotations

import pytest
from textual.widgets import Checkbox, Input, Select, Static

from lancher_code.config import load_config, resolve_config_bootstrap_state
from lancher_code.tui_views.bootstrap import ConfigBootstrapApp, ConfigBootstrapTUI


def test_resolve_config_bootstrap_state_prefers_global_config_path(tmp_path) -> None:
    home_dir = tmp_path / "home"
    cwd = tmp_path / "project"
    cwd.mkdir()
    (cwd / "lancher.yaml").write_text("provider: {}", encoding="utf-8")

    state = resolve_config_bootstrap_state(home_dir=home_dir, cwd=cwd)

    assert state.config_path == (home_dir / ".lancher" / "lancher.yaml").resolve()
    assert state.needs_setup is True
    assert state.legacy_config_path == (cwd / "lancher.yaml").resolve()
    assert state.legacy_config_exists is True


def test_resolve_config_bootstrap_state_skips_setup_when_global_config_exists(tmp_path) -> None:
    home_dir = tmp_path / "home"
    config_path = home_dir / ".lancher" / "lancher.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("provider: {}", encoding="utf-8")

    state = resolve_config_bootstrap_state(home_dir=home_dir)

    assert state.config_path == config_path.resolve()
    assert state.needs_setup is False


@pytest.mark.asyncio
async def test_config_bootstrap_app_saves_minimal_required_fields(tmp_path) -> None:
    config_path = tmp_path / "home" / ".lancher" / "lancher.yaml"
    app = ConfigBootstrapApp(config_path)

    async with app.run_test() as pilot:
        app.query_one("#model-input", Input).value = "gpt-4.1-mini"
        app.query_one("#api-key-input", Input).value = "test-key"
        app._save()
        await pilot.pause(0.05)

    config = load_config(config_path)
    assert config.provider.protocol == "openai"
    assert config.provider.model == "gpt-4.1-mini"
    assert config.provider.base_url == "https://api.openai.com/v1"
    assert config.provider.api_key == "test-key"
    assert config.provider.timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_config_bootstrap_app_can_save_claude_thinking_settings(tmp_path) -> None:
    config_path = tmp_path / "home" / ".lancher" / "lancher.yaml"
    app = ConfigBootstrapApp(config_path)

    async with app.run_test() as pilot:
        protocol_select = app.query_one("#protocol-select", Select)
        protocol_select.value = "claude"
        await pilot.pause(0.05)

        app.query_one("#model-input", Input).value = "claude-sonnet"
        app.query_one("#api-key-input", Input).value = "test-key"
        app.query_one("#thinking-enabled", Checkbox).value = True
        app.query_one("#thinking-budget-input", Input).value = "2048"
        app._save()
        await pilot.pause(0.05)

    config = load_config(config_path)
    assert config.provider.protocol == "claude"
    assert config.provider.base_url == "https://api.anthropic.com/v1"
    assert config.provider.thinking is not None
    assert config.provider.thinking.enabled is True
    assert config.provider.thinking.budget_tokens == 2048


@pytest.mark.asyncio
async def test_config_bootstrap_app_shows_validation_error_without_writing_file(tmp_path) -> None:
    config_path = tmp_path / "home" / ".lancher" / "lancher.yaml"
    app = ConfigBootstrapApp(config_path)

    async with app.run_test() as pilot:
        app.query_one("#api-key-input", Input).value = "test-key"
        app._save()
        await pilot.pause(0.05)

        error_widget = app.query_one("#bootstrap-error", Static)
        assert error_widget.display is True
        assert "model" in str(error_widget.render())

    assert not config_path.exists()


@pytest.mark.asyncio
async def test_config_bootstrap_tui_returns_false_when_cancelled(tmp_path) -> None:
    tui = ConfigBootstrapTUI(tmp_path / "home" / ".lancher" / "lancher.yaml")

    async def fake_run_async() -> int:
        return 1

    tui._app.run_async = fake_run_async  # type: ignore[method-assign]

    assert await tui.run() is False
