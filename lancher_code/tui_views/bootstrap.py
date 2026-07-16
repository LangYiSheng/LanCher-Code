from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static

from lancher_code.config import write_config_data
from lancher_code.errors import ConfigError
from lancher_code.mcp.template import ensure_user_mcp_config

PROTOCOL_OPTIONS = [("OpenAI", "openai"), ("Claude", "claude")]
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "claude": "https://api.anthropic.com/v1",
}
MODEL_PLACEHOLDERS = {
    "openai": "例如 gpt-4.1-mini",
    "claude": "例如 claude-sonnet",
}


class ConfigBootstrapApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
        color: #f2f2f2;
    }

    #bootstrap-scroll {
        width: 100%;
        height: 100%;
        align-horizontal: center;
    }

    #bootstrap-root {
        width: 100%;
        max-width: 76;
        height: auto;
        padding: 1;
    }

    #bootstrap-header {
        width: 1fr;
        height: auto;
        border-left: wide #73b6ff;
        padding-left: 1;
        margin-bottom: 2;
    }

    #bootstrap-title {
        color: #73b6ff;
        text-style: bold;
        margin-bottom: 0;
    }

    #bootstrap-copy {
        color: #c8d5e3;
        margin-bottom: 1;
    }

    #bootstrap-path {
        color: #97adc7;
        height: auto;
    }

    #bootstrap-error {
        color: #ff7b72;
        border-left: wide #ff7b72;
        padding: 0 1;
        margin-bottom: 2;
        width: 1fr;
        height: auto;
        display: none;
    }

    .field {
        margin-bottom: 1;
        width: 1fr;
        height: auto;
    }

    .field-label {
        color: #c8d5e3;
        margin-bottom: 0;
    }

    .field-input {
        width: 1fr;
        border: tall #4b6f97;
        background: transparent;
    }

    .field-input:focus {
        border: tall #73b6ff;
    }

    #advanced-panel {
        margin: 1 0;
    }

    #claude-thinking {
        margin-top: 1;
        height: auto;
    }

    #actions {
        margin-top: 2;
        height: auto;
        width: 1fr;
    }

    #actions Button {
        min-width: 16;
    }

    #cancel-button {
        margin-left: 1;
    }

    #actions.-narrow {
        layout: vertical;
    }

    #actions.-narrow Button {
        width: 1fr;
        margin: 0 0 1 0;
    }
    """

    NARROW_WIDTH = 48

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="bootstrap-scroll"):
            with Vertical(id="bootstrap-root"):
                with Vertical(id="bootstrap-header"):
                    yield Static("LanCher Code", id="bootstrap-title")
                    yield Static("首次启动 · 配置模型供应商", id="bootstrap-copy")
                    yield Static(f"配置保存位置：{self._config_path}", id="bootstrap-path")
                yield Static("", id="bootstrap-error")

                with Vertical(classes="field"):
                    yield Static("提供商协议", classes="field-label")
                    yield Select(
                        PROTOCOL_OPTIONS,
                        value="openai",
                        allow_blank=False,
                        id="protocol-select",
                        classes="field-input",
                    )

                with Vertical(classes="field"):
                    yield Static("模型名称", classes="field-label")
                    yield Input(
                        placeholder=MODEL_PLACEHOLDERS["openai"],
                        id="model-input",
                        classes="field-input",
                    )

                with Vertical(classes="field"):
                    yield Static("Base URL", classes="field-label")
                    yield Input(
                        value=DEFAULT_BASE_URLS["openai"],
                        id="base-url-input",
                        classes="field-input",
                    )

                with Vertical(classes="field"):
                    yield Static("API Key", classes="field-label")
                    yield Input(password=True, id="api-key-input", classes="field-input")

                with Collapsible(title="高级选项", collapsed=True, id="advanced-panel"):
                    with Vertical(classes="field"):
                        yield Static("请求超时（秒）", classes="field-label")
                        yield Input(value="60", id="timeout-input", classes="field-input")

                    with Vertical(id="claude-thinking"):
                        yield Checkbox("启用 Claude thinking", id="thinking-enabled")
                        with Vertical(classes="field"):
                            yield Static("thinking budget_tokens（可选）", classes="field-label")
                            yield Input(
                                placeholder="例如 2048",
                                id="thinking-budget-input",
                                classes="field-input",
                            )

                with Horizontal(id="actions"):
                    yield Button("保存并启动", variant="primary", id="save-button")
                    yield Button("取消", id="cancel-button")

    def on_mount(self) -> None:
        self.query_one("#model-input", Input).focus()
        self._sync_protocol_fields("openai")
        self._refresh_responsive_layout()

    def on_resize(self) -> None:
        self._refresh_responsive_layout()

    def _refresh_responsive_layout(self) -> None:
        self.query_one("#actions", Horizontal).set_class(
            self.size.width < self.NARROW_WIDTH,
            "-narrow",
        )

    @on(Select.Changed, "#protocol-select")
    def handle_protocol_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._sync_protocol_fields(event.value)

    @on(Button.Pressed, "#save-button")
    def handle_save_pressed(self) -> None:
        self._save()

    @on(Button.Pressed, "#cancel-button")
    def handle_cancel_pressed(self) -> None:
        self.exit(1)

    def _sync_protocol_fields(self, protocol: str) -> None:
        model_input = self.query_one("#model-input", Input)
        base_url_input = self.query_one("#base-url-input", Input)
        thinking_group = self.query_one("#claude-thinking", Vertical)

        model_input.placeholder = MODEL_PLACEHOLDERS[protocol]
        if not base_url_input.value.strip() or base_url_input.value in DEFAULT_BASE_URLS.values():
            base_url_input.value = DEFAULT_BASE_URLS[protocol]
        thinking_group.display = protocol == "claude"
        if protocol != "claude":
            self.query_one("#thinking-enabled", Checkbox).value = False
            self.query_one("#thinking-budget-input", Input).value = ""

    def _save(self) -> None:
        try:
            raw_data = self._build_raw_config()
            write_config_data(self._config_path, raw_data)
            ensure_user_mcp_config(home_dir=self._config_path.parent.parent)
        except ConfigError as exc:
            self._show_error(exc.user_message)
            return

        self.exit(0)

    def _build_raw_config(self) -> dict[str, Any]:
        protocol = self._read_select_value()
        timeout_seconds = self._parse_positive_float(
            self.query_one("#timeout-input", Input).value,
            "timeout_seconds",
        )

        provider: dict[str, Any] = {
            "protocol": protocol,
            "model": self.query_one("#model-input", Input).value,
            "base_url": self.query_one("#base-url-input", Input).value,
            "api_key": self.query_one("#api-key-input", Input).value,
            "timeout_seconds": timeout_seconds,
        }

        if protocol == "claude":
            thinking_enabled = self.query_one("#thinking-enabled", Checkbox).value
            budget_raw = self.query_one("#thinking-budget-input", Input).value.strip()
            if thinking_enabled or budget_raw:
                thinking: dict[str, Any] = {"enabled": thinking_enabled}
                if budget_raw:
                    thinking["budget_tokens"] = self._parse_positive_int(budget_raw, "thinking.budget_tokens")
                provider["thinking"] = thinking

        return {"provider": provider}

    def _read_select_value(self) -> str:
        value = self.query_one("#protocol-select", Select).value
        if not isinstance(value, str):
            raise ConfigError("protocol 是必填字符串。")
        return value

    @staticmethod
    def _parse_positive_float(value: str, key: str) -> float:
        raw_value = value.strip()
        try:
            parsed = float(raw_value)
        except ValueError as exc:
            raise ConfigError(f"{key} 必须是正数。") from exc
        if parsed <= 0:
            raise ConfigError(f"{key} 必须是正数。")
        return parsed

    @staticmethod
    def _parse_positive_int(value: str, key: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ConfigError(f"{key} 必须是正整数。") from exc
        if parsed <= 0:
            raise ConfigError(f"{key} 必须是正整数。")
        return parsed

    def _show_error(self, message: str) -> None:
        error_widget = self.query_one("#bootstrap-error", Static)
        error_widget.update(message)
        error_widget.display = True
        self.call_after_refresh(
            error_widget.scroll_visible,
            animate=False,
            top=True,
            force=True,
            immediate=True,
        )


class ConfigBootstrapTUI:
    def __init__(self, config_path: Path) -> None:
        self._app = ConfigBootstrapApp(config_path)

    async def run(self) -> bool:
        result = await self._app.run_async()
        return result == 0
