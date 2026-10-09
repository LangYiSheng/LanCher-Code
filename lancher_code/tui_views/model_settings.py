from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Callable
from uuid import uuid4

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from lancher_code.model_catalog import iter_model_refs, new_entry_id
from lancher_code.models import AppConfig, ModelDefinition, ProviderDefinition, ThinkingConfig


PROTOCOLS = (("OpenAI", "openai"), ("Anthropic", "claude"))
class DeleteProviderScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "取消")]
    CSS = """
    DeleteProviderScreen { align: center middle; background: #08111b 70%; }
    #delete-provider-box { width: 64; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; background: #0f1a26; border: solid #4b6f97; }
    #delete-provider-box Static { height: auto; }
    #delete-provider-actions { height: auto; margin-top: 1; align-horizontal: right; }
    #delete-provider-actions Button { min-width: 8; margin-left: 1; }
    """

    def __init__(self, name: str, models: list[str]) -> None:
        super().__init__()
        self._name = name
        self._models = models

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="delete-provider-box"):
            yield Static(f"删除供应商“{self._name}”？", markup=False)
            yield Static("同时删除以下模型：\n" + ("\n".join(self._models) or "（没有模型）"), markup=False)
            with Horizontal(id="delete-provider-actions"):
                yield Button("取消", id="delete-provider-cancel")
                yield Button("删除", variant="error", id="delete-provider-confirm")

    @on(Button.Pressed, "#delete-provider-cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#delete-provider-confirm")
    def confirm(self) -> None:
        self.dismiss(True)


class ModelSettingsEditor(Vertical):
    """只编辑配置草稿；连接参数保留继承关系和环境变量原文。"""

    DEFAULT_CSS = """
    ModelSettingsEditor .catalog { width: 38%; min-width: 24; }
    ModelSettingsEditor .model-editor { width: 1fr; min-width: 0; padding-left: 2; }
    ModelSettingsEditor .catalog-title, ModelSettingsEditor .editor-title { height: auto; color: #73b6ff; text-style: bold; }
    ModelSettingsEditor .inherit-note { height: auto; color: #7f9ab8; }
    ModelSettingsEditor .model-id { height: auto; color: #7f9ab8; margin-bottom: 1; }
    ModelSettingsEditor .catalog DataTable { height: 1fr; min-height: 5; }
    ModelSettingsEditor .row-actions { height: auto; min-height: 3; }
    ModelSettingsEditor .row-actions Button { min-width: 8; width: 1fr; margin-left: 0; }
    ModelSettingsEditor .field-label { height: auto; }
    ModelSettingsEditor.-narrow .split { layout: vertical; overflow-y: auto; }
    ModelSettingsEditor.-narrow .catalog { width: 100%; min-width: 0; height: auto; }
    ModelSettingsEditor.-narrow .catalog DataTable { height: 7; }
    ModelSettingsEditor.-narrow .model-editor { width: 100%; min-width: 0; height: auto; padding-left: 0; }
    """

    def __init__(self, show_error: Callable[[str], None]) -> None:
        super().__init__(id="page-model", classes="settings-page")
        self._show_error = show_error
        self.config: AppConfig | None = None
        self.provider_id: str | None = None
        self.model_id: str | None = None

    def compose(self) -> ComposeResult:
        yield from self._field("新会话默认模型", Select([], prompt="先添加模型", id="default-model"))
        with Horizontal(classes="split"):
            with Vertical(classes="catalog"):
                yield Static("供应商", classes="catalog-title")
                yield DataTable(id="providers-table", cursor_type="row")
                with Horizontal(classes="row-actions"):
                    yield Button("新增", id="provider-new")
                    yield Button("删除", variant="error", id="provider-delete")
                yield Static("该供应商的模型", classes="catalog-title")
                yield DataTable(id="models-table", cursor_type="row")
                with Horizontal(classes="row-actions"):
                    yield Button("新增", id="model-new")
                    yield Button("删除", variant="error", id="model-delete")
            with VerticalScroll(classes="model-editor"):
                with Vertical(id="provider-fields", classes="field"):
                    yield Static("供应商连接配置", classes="editor-title")
                    yield Static("", id="provider-id", classes="model-id", markup=False)
                    yield from self._field("供应商名称", Input(id="provider-name"))
                    yield from self._field("默认调用协议", Select(PROTOCOLS, value="openai", allow_blank=False, id="provider-protocol"))
                    yield from self._field("默认 Base URL", Input(id="provider-base-url"))
                    yield from self._field("默认 API Key（留空保留原值）", Input(password=True, id="provider-api-key"))
                    yield from self._field("请求超时（秒）", Input(id="provider-timeout", type="number"))
                with Vertical(id="model-fields", classes="field"):
                    yield Static("模型配置", classes="editor-title")
                    yield Static("", id="model-id", classes="model-id", markup=False)
                    yield from self._field("API 模型名称", Input(id="model-name"))
                    yield from self._field("显示名称（可选）", Input(id="model-display-name"))
                    for name, label, widget in (
                        ("protocol", "调用协议", Select(PROTOCOLS, value="openai", allow_blank=False, id="model-protocol")),
                        ("base-url", "Base URL", Input(id="model-base-url")),
                        ("api-key", "API Key（自定义时留空保留原值）", Input(password=True, id="model-api-key")),
                        ("timeout", "请求超时（秒）", Input(id="model-timeout", type="number")),
                    ):
                        with Vertical(classes="field"):
                            yield Static(label, classes="field-label")
                            yield Checkbox("继承供应商", value=True, id=f"inherit-{name}", classes="model-inherit")
                            yield Static("", id=f"source-{name}", classes="inherit-note", markup=False)
                            yield widget
                    yield Static("模型高级设置", classes="editor-title")
                    yield from self._field("上下文窗口（tokens，留空使用协议默认值）", Input(id="model-context-window", type="integer"))
                    yield Checkbox("启用 Anthropic thinking", id="model-thinking")
                    yield from self._field("Thinking budget tokens（可选）", Input(id="model-thinking-budget", type="integer"))
                with Horizontal(classes="row-actions"):
                    yield Button("应用条目", variant="primary", id="catalog-apply")
                yield Static("应用条目只更新草稿；点击底部“保存”后生效。", classes="inherit-note")

    @staticmethod
    def _field(label: str, widget: Any) -> ComposeResult:
        with Vertical(classes="field"):
            yield Static(label, classes="field-label")
            yield widget

    def on_mount(self) -> None:
        self.query_one("#providers-table", DataTable).add_columns("名称", "ID")
        self.query_one("#models-table", DataTable).add_columns("模型", "ID")

    def load_config(self, config: AppConfig) -> None:
        self.config = config
        self.provider_id = next(iter(config.providers), None)
        provider = config.providers.get(self.provider_id) if self.provider_id else None
        self.model_id = next(iter(provider.models), None) if provider else None
        self._refresh_catalog()
        self._load_fields()

    def _refresh_catalog(self) -> None:
        if self.config is None:
            return
        providers = self.query_one("#providers-table", DataTable)
        providers.clear()
        for provider_id, provider in self.config.providers.items():
            providers.add_row(Text(provider.name), provider_id, key=provider_id)
        if self.provider_id in self.config.providers:
            providers.move_cursor(row=list(self.config.providers).index(self.provider_id))
        models = self.query_one("#models-table", DataTable)
        models.clear()
        if self.provider_id in self.config.providers:
            for model_id, model in self.config.providers[self.provider_id].models.items():
                label = model.display_name or model.model_name or "（待填写）"
                ref = f"{self.provider_id}/{model_id}"
                models.add_row(Text(("★ " if ref == self.config.default_model else "") + label), model_id, key=model_id)
            if self.model_id in self.config.providers[self.provider_id].models:
                models.move_cursor(row=list(self.config.providers[self.provider_id].models).index(self.model_id))
        with self.prevent(Select.Changed):
            select = self.query_one("#default-model", Select)
            refs = list(iter_model_refs(self.config))
            options = []
            for ref in refs:
                provider_id, model_id = ref.split("/", 1)
                provider = self.config.providers[provider_id]
                model = provider.models[model_id]
                label = model.display_name or f"{model.model_name or '（待填写）'} ({provider.name})"
                options.append((Text(f"{label} · {ref}"), ref))
            select.set_options(options)
            select.value = self.config.default_model if self.config.default_model in refs else Select.BLANK

    def _load_fields(self) -> None:
        assert self.config is not None
        provider = self.config.providers.get(self.provider_id) if self.provider_id else None
        self.query_one("#provider-fields").display = provider is not None
        model = provider.models.get(self.model_id) if provider and self.model_id else None
        self.query_one("#model-fields").display = model is not None
        if provider is None:
            return
        with self.prevent(Select.Changed, Checkbox.Changed):
            self.query_one("#provider-id", Static).update(f"ID：{self.provider_id}（改名不改变 ID）")
            self.query_one("#provider-name", Input).value = provider.name
            self.query_one("#provider-protocol", Select).value = provider.protocol
            self.query_one("#provider-base-url", Input).value = provider.base_url
            self.query_one("#provider-api-key", Input).value = ""
            self.query_one("#provider-timeout", Input).value = str(provider.timeout_seconds)
            if model is None:
                return
            self.query_one("#model-id", Static).update(f"引用：{self.provider_id}/{self.model_id}")
            self.query_one("#model-name", Input).value = model.model_name
            self.query_one("#model-display-name", Input).value = model.display_name
            for field, attribute in (("protocol", "protocol"), ("base-url", "base_url"), ("api-key", "api_key"), ("timeout", "timeout_seconds")):
                own = getattr(model, attribute)
                inherited = getattr(provider, attribute)
                self.query_one(f"#inherit-{field}", Checkbox).value = own is None
                widget = self.query_one(f"#model-{field}")
                widget.value = "" if field == "api-key" else str(own if own is not None else inherited)
            self.query_one("#model-context-window", Input).value = str(model.context_window) if model.context_window is not None else ""
            self.query_one("#model-thinking", Checkbox).value = bool(model.thinking and model.thinking.enabled)
            self.query_one("#model-thinking-budget", Input).value = str(model.thinking.budget_tokens or "") if model.thinking else ""
        self._sync_inheritance()

    def _sync_inheritance(self) -> None:
        if self.config is None or self.provider_id not in self.config.providers:
            return
        provider = self.config.providers[self.provider_id]
        name = self.query_one("#provider-name", Input).value.strip() or provider.name
        for field, attribute in (("protocol", "protocol"), ("base-url", "base_url"), ("api-key", "api_key"), ("timeout", "timeout_seconds")):
            inherited = self.query_one(f"#inherit-{field}", Checkbox).value
            widget = self.query_one(f"#model-{field}")
            widget.disabled = inherited
            value = self.query_one(f"#provider-{field}").value
            if field == "api-key":
                value = "已配置（隐藏）" if value or provider.api_key else "未配置"
            elif inherited:
                widget.value = value
            if field == "protocol":
                value = "Anthropic" if value == "claude" else "OpenAI"
            self.query_one(f"#source-{field}", Static).update(f"{'继承自' if inherited else '可恢复继承'} {name}：{value}")

    @on(Input.Changed)
    def provider_field_changed(self, event: Input.Changed) -> None:
        if (event.input.id or "").startswith("provider-"):
            self._sync_inheritance()

    @on(Select.Changed, "#provider-protocol")
    def provider_protocol_changed(self) -> None:
        self._sync_inheritance()

    @on(Checkbox.Changed, ".model-inherit")
    def inheritance_changed(self) -> None:
        self._sync_inheritance()

    def collect(self) -> None:
        if self.config is None or self.provider_id not in self.config.providers:
            return
        old_provider = self.config.providers[self.provider_id]
        provider = deepcopy(old_provider)
        provider.name = self.query_one("#provider-name", Input).value.strip()
        if not provider.name:
            raise ValueError("请填写供应商名称。")
        provider.protocol = self.query_one("#provider-protocol", Select).value
        provider.base_url = self.query_one("#provider-base-url", Input).value.strip()
        provider.api_key = self.query_one("#provider-api-key", Input).value.strip() or old_provider.api_key
        provider.timeout_seconds = self._positive_number("provider-timeout", "供应商请求超时")
        if self.model_id in provider.models:
            old_model = provider.models[self.model_id]
            model = deepcopy(old_model)
            model.model_name = self.query_one("#model-name", Input).value.strip()
            model.display_name = self.query_one("#model-display-name", Input).value.strip()
            for field, attribute in (("protocol", "protocol"), ("base-url", "base_url"), ("api-key", "api_key"), ("timeout", "timeout_seconds")):
                if self.query_one(f"#inherit-{field}", Checkbox).value:
                    value = None
                elif field == "timeout":
                    value = self._positive_number("model-timeout", "模型请求超时")
                else:
                    value = self.query_one(f"#model-{field}").value
                    value = value.strip() if isinstance(value, str) else value
                    if field == "api-key":
                        value = value or old_model.api_key or ""
                setattr(model, attribute, value)
            window = self.query_one("#model-context-window", Input).value.strip()
            model.context_window = self._positive_number("model-context-window", "上下文窗口", integer=True) if window else None
            enabled = self.query_one("#model-thinking", Checkbox).value
            budget = self.query_one("#model-thinking-budget", Input).value.strip()
            model.thinking = ThinkingConfig(enabled=enabled, budget_tokens=self._positive_number("model-thinking-budget", "Thinking budget", integer=True) if budget else None) if enabled or budget or old_model.thinking is not None else None
            provider.models[self.model_id] = model
        self.config.providers[self.provider_id] = provider

    def _positive_number(self, widget_id: str, label: str, *, integer: bool = False) -> Any:
        try:
            text = self.query_one(f"#{widget_id}", Input).value.strip()
            value = int(text) if integer else float(text)
            if value <= 0 or not math.isfinite(value):
                raise ValueError
            return value
        except ValueError as exc:
            raise ValueError(f"{label}必须是正{'整数' if integer else '数'}。") from exc

    def _collect_or_error(self) -> bool:
        try:
            self.collect()
        except ValueError as exc:
            self._show_error(str(exc))
            return False
        self._show_error("")
        return True

    @on(Button.Pressed, "#catalog-apply")
    def apply_entry(self) -> None:
        if self._collect_or_error():
            self._refresh_catalog()
            self._load_fields()

    @on(DataTable.RowSelected, "#providers-table")
    def select_provider(self, event: DataTable.RowSelected) -> None:
        if not self._collect_or_error():
            return
        self.provider_id = str(event.row_key.value)
        self.model_id = next(iter(self.config.providers[self.provider_id].models), None)
        self._refresh_catalog()
        self._load_fields()

    @on(DataTable.RowSelected, "#models-table")
    def select_model(self, event: DataTable.RowSelected) -> None:
        if self._collect_or_error():
            self.model_id = str(event.row_key.value)
            self._refresh_catalog()
            self._load_fields()

    @on(Select.Changed, "#default-model")
    def select_default(self, event: Select.Changed) -> None:
        if self.config is not None and event.value in list(iter_model_refs(self.config)):
            self.config.default_model = str(event.value)
            self._refresh_catalog()

    @on(Button.Pressed, "#provider-new")
    def new_provider(self) -> None:
        if self.config is None or not self._collect_or_error():
            return
        # UUID 后缀避免删除后重建复用旧会话保存的引用。
        self.provider_id = new_entry_id(f"provider-{uuid4().hex[:12]}", self.config.providers)
        self.config.providers[self.provider_id] = ProviderDefinition(name="新供应商", protocol="openai", base_url="https://api.openai.com/v1", api_key="", models={})
        self.model_id = None
        self._refresh_catalog()
        self._load_fields()
        self.query_one("#provider-name", Input).focus()

    @on(Button.Pressed, "#model-new")
    def new_model(self) -> None:
        if self.config is None or self.provider_id not in self.config.providers or not self._collect_or_error():
            return
        provider = self.config.providers[self.provider_id]
        self.model_id = new_entry_id(f"model-{uuid4().hex[:12]}", provider.models)
        provider.models[self.model_id] = ModelDefinition(model_name="")
        if not self.config.default_model:
            self.config.default_model = f"{self.provider_id}/{self.model_id}"
        self._refresh_catalog()
        self._load_fields()
        self.query_one("#model-name", Input).focus()

    @on(Button.Pressed, "#model-delete")
    def delete_model(self) -> None:
        if self.config is None or self.provider_id not in self.config.providers or not self.model_id:
            return
        if self.config.default_model == f"{self.provider_id}/{self.model_id}":
            self._show_error("请先选择另一个默认模型，再删除此模型。")
            return
        provider = self.config.providers[self.provider_id]
        provider.models.pop(self.model_id, None)
        self.model_id = next(iter(provider.models), None)
        self._refresh_catalog()
        self._load_fields()
        self._show_error("")

    @on(Button.Pressed, "#provider-delete")
    def delete_provider(self) -> None:
        if self.config is None or self.provider_id not in self.config.providers:
            return
        if self.config.default_model.startswith(f"{self.provider_id}/"):
            self._show_error("请先选择其他供应商的默认模型，再删除此供应商。")
            return
        provider_id = self.provider_id
        provider = self.config.providers[provider_id]
        labels = [f"{model.display_name or model.model_name} ({provider_id}/{model_id})" for model_id, model in provider.models.items()]
        self.app.push_screen(DeleteProviderScreen(provider.name, labels), lambda confirmed: self._after_delete(provider_id, confirmed))

    def _after_delete(self, provider_id: str, confirmed: bool | None) -> None:
        if not confirmed or self.config is None:
            return
        self.config.providers.pop(provider_id, None)
        self.provider_id = next(iter(self.config.providers), None)
        provider = self.config.providers.get(self.provider_id) if self.provider_id else None
        self.model_id = next(iter(provider.models), None) if provider else None
        self._refresh_catalog()
        self._load_fields()
        self._show_error("")
