from __future__ import annotations

from math import isfinite
import os
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static

from lancher_code.config.writer import write_config_data
from lancher_code.config.loader import load_config_data
from lancher_code.tui.theme import apply_theme
from lancher_code.errors import ConfigError
from lancher_code.mcp.template import ensure_user_mcp_config
from lancher_code.providers.catalog import new_entry_id
from lancher_code.providers.reasoning import API_MODE_OPTIONS, EFFORT_LABELS, EFFORT_LEVELS, reasoning_capabilities
from lancher_code.providers.discovery import ModelDiscoveryClient, discovery_connection
from lancher_code.providers.models import ProviderDefinition
from lancher_code.providers.metadata import bound_metadata, serialize_metadata
from lancher_code.tui.model_discovery import ModelDiscoveryScreen

PROTOCOL_OPTIONS = [("OpenAI", "openai"), ("Anthropic", "claude")]
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
    Screen { layout: vertical; align-horizontal: center; color: $text; background: $background; }
    #bootstrap-scroll { width: 100%; height: 1fr; align-horizontal: center; }
    #bootstrap-root { width: 100%; max-width: 76; height: auto; padding: 1 2; }
    #bootstrap-header { height: auto; margin-bottom: 1; }
    #bootstrap-title { color: $text; text-style: bold; height: auto; }
    #bootstrap-copy { color: $text-muted; height: auto; }
    #bootstrap-step { color: $text; text-style: bold; height: auto; margin: 1 0; }
    #bootstrap-error { color: $error; height: auto; display: none; margin-bottom: 1; }
    .setup-page { height: auto; display: none; }
    .setup-page.-active { display: block; }
    .field { width: 1fr; height: auto; margin-bottom: 1; }
    .field-label { color: $text; height: auto; }
    .field-input { width: 1fr; height: 3; color: $text; background: $background; border: none; }
    Input.field-input { padding: 0 1; border-bottom: solid $panel; }
    Input.field-input > .input--placeholder { color: $text-muted; }
    Input.field-input:focus { color: $background; background: $foreground; background-tint: transparent; border-bottom: solid $foreground; }
    Input.field-input:focus > .input--placeholder { color: $background 70%; }
    Input.field-input:focus > .input--cursor { color: $text; background: $background; }
    Select.field-input > SelectCurrent { color: $text; background: $background; border: none; border-bottom: solid $panel; padding: 0 1; }
    Select.field-input > SelectCurrent #label, Select.field-input > SelectCurrent .arrow { color: $text; }
    Select.field-input:focus > SelectCurrent { color: $background; background: $foreground; background-tint: transparent; border-bottom: solid $foreground; }
    Select.field-input:focus > SelectCurrent #label, Select.field-input:focus > SelectCurrent .arrow { color: $background; }
    Select.field-input > SelectOverlay { background: $surface; border: solid $panel; }
    Select.field-input > SelectOverlay > .option-list--option-highlighted { color: $background; background: $foreground; text-style: none; }
    .setup-hint { color: $text-muted; height: auto; margin-bottom: 1; }
    Collapsible { background: transparent; border: none; padding: 0; }
    Collapsible:focus-within { background-tint: transparent; }
    Collapsible > Contents { padding: 1 0 0 1; }
    CollapsibleTitle { color: $text-muted; text-style: none; padding: 0; }
    CollapsibleTitle:hover { color: $text; background: $surface; }
    CollapsibleTitle:focus { color: $background; background: $foreground; text-style: none; }
    Checkbox { height: 1; border: none; padding: 0; color: $text; background: transparent; }
    Checkbox:focus { border: none; color: $background; background: $foreground; background-tint: transparent; }
    Checkbox:focus > .toggle--label { color: $background; background: $foreground; text-style: none; }
    #bootstrap-summary, #bootstrap-path { height: auto; margin-bottom: 1; }
    #bootstrap-path { color: $text-muted; }
    #bootstrap-footer { width: 100%; height: auto; align-horizontal: center; }
    #actions { width: 100%; max-width: 76; height: auto; padding: 0 2; }
    #actions Button { min-width: 10; height: 3; margin-right: 1; color: $text; background: transparent; border: none; text-style: none; }
    #actions #save-button { color: $text; background: $surface; text-style: bold; }
    #actions Button:hover { color: $text; background: $surface; border: none; }
    #actions Button:focus, #actions #save-button:focus { color: $background; background: $foreground; background-tint: transparent; border: none; text-style: none; }
    #actions.-narrow { padding: 0 2; }
    #actions.-narrow Button { min-width: 8; margin-right: 0; }
    #bootstrap-help { width: 100%; max-width: 76; height: 1; color: $text-muted; padding: 0 2; }
    """
    BINDINGS = [("escape", "back", "返回"), ("ctrl+s", "continue", "继续")]

    NARROW_WIDTH = 48

    def __init__(self, config_path: Path, *, model_discovery: ModelDiscoveryClient | None = None) -> None:
        super().__init__()
        self._config_path = config_path
        self._step = 0
        self._model_discovery = model_discovery or ModelDiscoveryClient()
        self._discovered_models = []
        self._discovery_generation = 0
        self._discovery_worker = None
        self._bootstrap_screen = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="bootstrap-scroll"):
            with Vertical(id="bootstrap-root"):
                with Vertical(id="bootstrap-header"):
                    yield Static("LanCher Code", id="bootstrap-title")
                    yield Static("首次配置 · 连接供应商，再添加模型", id="bootstrap-copy")
                    yield Static("", id="bootstrap-step")
                yield Static("", id="bootstrap-error", markup=False)
                with Vertical(id="setup-connection", classes="setup-page"):
                    yield Static("供应商保存连接地址和 API Key，可供多个模型共用。", classes="setup-hint")
                    yield Static("供应商名称", classes="field-label")
                    yield Input(value="自定义供应商", placeholder="例如 DeepSeek", id="provider-name-input", classes="field-input")
                    yield Static("连接协议", classes="field-label")
                    yield Select(PROTOCOL_OPTIONS, value="openai", allow_blank=False, id="protocol-select", classes="field-input")
                    yield Static("API 地址（Base URL）", classes="field-label")
                    yield Input(value=DEFAULT_BASE_URLS["openai"], id="base-url-input", classes="field-input")
                    yield Static("API Key", classes="field-label")
                    yield Input(password=True, placeholder="支持 ${环境变量名}", id="api-key-input", classes="field-input")
                    with Collapsible(title="高级连接选项", collapsed=True, id="advanced-panel"):
                        yield Static("OpenAI 接口形式", classes="field-label")
                        yield Select(API_MODE_OPTIONS, value="auto", allow_blank=False, id="api-mode-select", classes="field-input")
                        yield Static("请求超时（秒）", classes="field-label")
                        yield Input(value="60", id="timeout-input", classes="field-input")
                with Vertical(id="setup-model", classes="setup-page"):
                    yield Static("", id="setup-model-parent", classes="setup-hint", markup=False)
                    yield Button("发现模型…", id="bootstrap-discover")
                    yield Static("", id="bootstrap-discovery-status", classes="setup-hint", markup=False)
                    yield Static("API 模型名称", classes="field-label")
                    yield Input(placeholder=MODEL_PLACEHOLDERS["openai"], id="model-input", classes="field-input")
                    yield Static("显示名称（可选）", classes="field-label")
                    yield Input(placeholder="例如 日常编程；留空使用 API 模型名称", id="model-display-input", classes="field-input")
                    with Collapsible(title="高级模型选项", collapsed=True, id="model-advanced-panel"):
                        yield Static("以后可在设置中添加模型，或为单个模型覆盖连接参数。", classes="setup-hint")
                        yield Static("单次总输出额度（token，可选）", classes="field-label")
                        yield Input(placeholder="默认 32768；包含思考、正文和工具参数", id="max-output-tokens-input", classes="field-input")
                        yield Static("推理强度（effort）", classes="field-label")
                        yield Select([("自动（推荐）", "auto")], value="auto", allow_blank=False, id="reasoning-effort-select", classes="field-input")
                        yield Static("", id="reasoning-help", classes="setup-hint", markup=False)
                        yield Static("思考轨迹显示可在外观设置中调整。", classes="setup-hint")
                with Vertical(id="setup-confirm", classes="setup-page"):
                    yield Static("", id="bootstrap-summary", markup=False)
                    yield Static("首次对话和新对话默认都会使用这个模型。以后可以分别更改。", classes="setup-hint")
                    yield Static(f"配置保存位置：{self._config_path}", id="bootstrap-path", markup=False)
                    yield Static("保存连接参数，不会在此步骤发起模型请求。", classes="setup-hint")
        with Vertical(id="bootstrap-footer"):
            with Horizontal(id="actions"):
                yield Button("取消", id="cancel-button")
                yield Button("上一步", id="back-button")
                yield Button("下一步", variant="primary", id="save-button")
            yield Static("Esc 返回 · Ctrl+S 继续", id="bootstrap-help")

    def on_mount(self) -> None:
        self._bootstrap_screen = self.screen
        apply_theme(self, "dark")
        self._sync_protocol_fields("openai")
        self._show_step(0)
        self._refresh_responsive_layout()

    def on_resize(self) -> None:
        self._refresh_responsive_layout()

    def on_unmount(self) -> None:
        self._discovery_generation += 1
        if self._discovery_worker is not None:
            self._discovery_worker.cancel()

    def _refresh_responsive_layout(self) -> None:
        self.query_one("#actions", Horizontal).set_class(self.size.width < self.NARROW_WIDTH, "-narrow")

    def _show_step(self, step: int) -> None:
        self._step = step
        pages = ("connection", "model", "confirm")
        for index, name in enumerate(pages):
            self.query_one(f"#setup-{name}").set_class(index == step, "-active")
        self.query_one("#bootstrap-step", Static).update(("1 / 3  连接供应商", "2 / 3  添加第一个模型", "3 / 3  确认并开始")[step])
        self.query_one("#bootstrap-error").display = False
        self.query_one("#back-button").display = step > 0
        self.query_one("#save-button", Button).label = "保存并开始" if step == 2 else "下一步"
        provider = self.query_one("#provider-name-input", Input).value.strip()
        model = self.query_one("#model-input", Input).value.strip()
        display = self.query_one("#model-display-input", Input).value.strip() or model
        self.query_one("#setup-model-parent", Static).update(f"所属供应商：{provider}\n此模型共用该供应商的地址和 API Key。")
        count = len(self._build_raw_config()['providers'][new_entry_id(provider, ())]['models']) if step == 2 else 0
        self.query_one("#bootstrap-summary", Static).update(f"供应商  {provider}\n  地址  {self.query_one('#base-url-input', Input).value}\n  已选择 {count} 个模型\n  首个模型  {display}\n      API 名称  {model}\n\n本次对话使用  {display}\n新对话默认    {display}")
        self.query_one("#bootstrap-scroll", VerticalScroll).scroll_home(animate=False)
        self.query_one(("#provider-name-input", "#model-input", "#save-button")[step]).focus()

    @on(Select.Changed, "#protocol-select")
    def handle_protocol_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._sync_protocol_fields(event.value)

    @on(Button.Pressed, "#save-button")
    def action_continue(self) -> None:
        if self._step == 2:
            self._save()
            return
        try:
            # 第一步只验证连接，避免后退后被隐藏的模型草稿错误困住。
            load_config_data(self._build_raw_config(connection_only=self._step == 0))
        except ConfigError as exc:
            self._show_error(exc.user_message)
            return
        connection_step = self._step == 0
        self._show_step(self._step + 1)
        if connection_step:
            self.discover_models()

    @on(Button.Pressed, "#back-button")
    def action_back(self) -> None:
        self._discovery_generation += 1
        if self._discovery_worker is not None:
            self._discovery_worker.cancel()
        self.query_one('#bootstrap-discover', Button).disabled = False
        if self._step:
            self._show_step(self._step - 1)
        else:
            self.exit(1)

    @on(Button.Pressed, "#cancel-button")
    def handle_cancel_pressed(self) -> None:
        self.exit(1)

    def _sync_protocol_fields(self, protocol: str) -> None:
        model_input = self.query_one("#model-input", Input)
        base_url_input = self.query_one("#base-url-input", Input)

        model_input.placeholder = MODEL_PLACEHOLDERS[protocol]
        if not base_url_input.value.strip() or base_url_input.value in DEFAULT_BASE_URLS.values():
            base_url_input.value = DEFAULT_BASE_URLS[protocol]
        self._sync_reasoning()

    @on(Input.Changed, "#model-input")
    @on(Input.Changed, "#base-url-input")
    def reasoning_model_changed(self) -> None:
        self._sync_reasoning()

    def _sync_reasoning(self) -> None:
        model = os.path.expandvars(self.query_one("#model-input", Input).value).strip()
        metadata = next((item for item in self._discovered_models if item.id == model), None)
        metadata = bound_metadata(metadata, protocol=self._read_select_value(), model=model,
            base_url=os.path.expandvars(self.query_one("#base-url-input", Input).value).strip().rstrip('/'),
            api_key=os.path.expandvars(self.query_one("#api-key-input", Input).value).strip())
        capabilities = reasoning_capabilities(self._read_select_value(), metadata)
        selector = self.query_one("#reasoning-effort-select", Select)
        levels = ("auto", *(capabilities.levels if capabilities.levels is not None else EFFORT_LEVELS[1:]))
        selected = selector.value if selector.value in levels else "auto"
        with self.prevent(Select.Changed):
            selector.set_options([(EFFORT_LABELS.get(level, level), level) for level in levels])
            selector.value = selected
        selector.disabled = capabilities.levels == ()
        self.query_one("#reasoning-help", Static).update(capabilities.description)

    @on(Input.Changed, '#base-url-input')
    @on(Input.Changed, '#api-key-input')
    @on(Input.Changed, '#timeout-input')
    @on(Select.Changed, '#protocol-select')
    @on(Select.Changed, '#api-mode-select')
    def discovery_connection_changed(self) -> None:
        self._discovery_generation += 1
        self._discovered_models = []
        if self._discovery_worker is not None:
            self._discovery_worker.cancel()
        self.query_one('#bootstrap-discover', Button).disabled = False
        self._sync_reasoning()

    def _discovery_provider(self) -> ProviderDefinition:
        return ProviderDefinition(name=self.query_one('#provider-name-input', Input).value.strip(),
            protocol=self._read_select_value(), base_url=self.query_one('#base-url-input', Input).value.strip(),
            api_key=self.query_one('#api-key-input', Input).value.strip(),
            timeout_seconds=self._parse_positive_float(self.query_one('#timeout-input', Input).value, 'timeout_seconds'),
            api_mode=str(self.query_one('#api-mode-select', Select).value))

    @on(Button.Pressed, '#bootstrap-discover')
    def discover_models(self) -> None:
        try:
            provider = self._discovery_provider()
            discovery_connection(provider)
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._discovery_generation += 1
        self.query_one('#bootstrap-discover', Button).disabled = True
        self.query_one('#bootstrap-discovery-status', Static).update('正在读取供应商的模型列表…')
        draft = (self.query_one('#model-input', Input).value, self.query_one('#model-display-input', Input).value)
        self._discovery_worker = self._discover_models(provider, self._discovery_generation, draft)

    @work(group='bootstrap-model-discovery', exclusive=True, exit_on_error=False)
    async def _discover_models(self, provider: ProviderDefinition, generation: int, draft: tuple[str, str]) -> None:
        try:
            result = await self._model_discovery.discover(provider, refresh=True)
        except Exception as exc:
            if self.is_mounted and generation == self._discovery_generation and self._step == 1:
                self.query_one('#bootstrap-discovery-status', Static).update(str(exc))
            return
        finally:
            if self.is_mounted and generation == self._discovery_generation:
                self.query_one('#bootstrap-discover', Button).disabled = False
        if not self.is_mounted or self._step != 1 or generation != self._discovery_generation or self.screen is not self._bootstrap_screen:
            return
        if not result.models:
            self.query_one('#bootstrap-discovery-status', Static).update(result.notice or '端点返回了空列表，可以手动填写 API 模型名称。')
            return
        model_name = self.query_one('#model-input', Input).value.strip()
        if draft != (self.query_one('#model-input', Input).value, self.query_one('#model-display-input', Input).value):
            self.query_one('#bootstrap-discovery-status', Static).update(f'已发现 {len(result.models)} 个模型；保留当前填写，点击发现模型查看。')
            return

        def selected(ids: list[str] | None) -> None:
            if ids is None or generation != self._discovery_generation:
                return
            self._discovered_models = [item for item in result.models if item.id in ids]
            if self._discovered_models:
                first = self._discovered_models[0]
                self.query_one('#model-input', Input).value = first.id
                self.query_one('#model-display-input', Input).value = first.display_name
            self._sync_reasoning()
            self.query_one('#bootstrap-discovery-status', Static).update(f'已选择 {len(ids)} 个模型，首个模型用于首次对话。')
        self.push_screen(ModelDiscoveryScreen(result, provider_name=provider.name,
                                              initial={model_name} if model_name else set()), selected)

    def _save(self) -> None:
        try:
            raw_data = self._build_raw_config()
            write_config_data(self._config_path, raw_data)
            ensure_user_mcp_config(home_dir=self._config_path.parent.parent)
        except ConfigError as exc:
            self._show_error(exc.user_message)
            return

        self.exit(0)

    def _build_raw_config(self, *, connection_only: bool = False) -> dict[str, Any]:
        protocol = self._read_select_value()
        timeout_seconds = self._parse_positive_float(
            self.query_one("#timeout-input", Input).value,
            "timeout_seconds",
        )

        provider_name = self.query_one("#provider-name-input", Input).value.strip()
        model_name = "validation" if connection_only else self.query_one("#model-input", Input).value.strip()
        provider_id = new_entry_id(provider_name, ())
        model_id = new_entry_id(model_name, ())
        model: dict[str, Any] = {
            "model_name": model_name,
            "display_name": self.query_one("#model-display-input", Input).value.strip(),
        }
        provider: dict[str, Any] = {
            "name": provider_name,
            "protocol": protocol,
            "base_url": self.query_one("#base-url-input", Input).value,
            "api_key": self.query_one("#api-key-input", Input).value,
            "timeout_seconds": timeout_seconds,
            "models": {model_id: model},
            "api_mode": self.query_one('#api-mode-select', Select).value,
        }

        if not connection_only:
            output_raw = self.query_one("#max-output-tokens-input", Input).value.strip()
            if output_raw:
                model["max_output_tokens"] = self._parse_positive_int(output_raw, "max_output_tokens")

        if not connection_only:
            model["reasoning_effort"] = self.query_one("#reasoning-effort-select", Select).value
            for metadata in self._discovered_models:
                metadata = bound_metadata(metadata, protocol=protocol, model=metadata.id,
                    base_url=os.path.expandvars(provider['base_url']).strip().rstrip('/'),
                    api_key=os.path.expandvars(provider['api_key']).strip())
                if metadata is None:
                    continue
                if metadata.id == model_name:
                    model['metadata'] = serialize_metadata(metadata)
                else:
                    discovered_id = new_entry_id(metadata.id, provider['models'])
                    provider['models'][discovered_id] = {'model_name': metadata.id, 'display_name': metadata.display_name,
                                                       'metadata': serialize_metadata(metadata)}

        return {"providers": {provider_id: provider}, "default_model": f"{provider_id}/{model_id}"}

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
        if not isfinite(parsed) or parsed <= 0:
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
        self.query_one("#bootstrap-scroll", VerticalScroll).scroll_home(animate=False)
        self.call_after_refresh(self.query_one("#bootstrap-scroll", VerticalScroll).scroll_home, animate=False)


class ConfigBootstrapTUI:
    def __init__(self, config_path: Path) -> None:
        self._app = ConfigBootstrapApp(config_path)

    async def run(self) -> bool:
        result = await self._app.run_async()
        return result == 0
