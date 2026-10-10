from __future__ import annotations

from copy import deepcopy
import math
from typing import Any
from uuid import uuid4

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static, Tree

from lancher_code.providers.catalog import iter_model_refs, model_display_name, new_entry_id
from lancher_code.config.models import AppConfig
from lancher_code.providers.models import ModelDefinition, ProviderDefinition, ThinkingConfig
from lancher_code.tui.theme import theme_palette

PROTOCOLS = (("OpenAI 兼容", "openai"), ("Anthropic", "claude"))


class DeleteProviderScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "取消")]
    CSS = """
    DeleteProviderScreen { align: center middle; background: $background 80%; }
    #delete-provider-box { width: 64; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: solid $panel; overflow-y: auto; }
    #delete-provider-box Static { height: auto; }
    #delete-provider-actions { height: auto; margin-top: 1; }
    #delete-provider-actions Button { min-width: 8; margin-right: 1; background: transparent; color: $text; text-style: none; border: none; }
    #delete-provider-actions Button:focus { background: $foreground; color: $background; text-style: bold; }
    DeleteProviderScreen.-narrow #delete-provider-actions { layout: vertical; }
    DeleteProviderScreen.-narrow #delete-provider-actions Button { width: 1fr; margin-right: 0; }
    """

    def __init__(self, name: str, models: list[str]) -> None:
        super().__init__()
        self._name, self._models = name, models

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-provider-box"):
            yield Static(f"删除供应商“{self._name}”？", markup=False)
            yield Static("同时删除以下模型：\n" + ("\n".join(self._models) or "（没有模型）"), markup=False)
            with Horizontal(id="delete-provider-actions"):
                yield Button("取消", id="delete-provider-cancel")
                yield Button("删除", variant="error", id="delete-provider-confirm")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 50, "-narrow")

    def on_resize(self) -> None:
        self.set_class(self.size.width < 50, "-narrow")

    @on(Button.Pressed, "#delete-provider-cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#delete-provider-confirm")
    def confirm(self) -> None:
        self.dismiss(True)


class ModelSettingsEditor(Vertical):
    """目录是已提交快照，表单是独立草稿；保存由设置页统一处理。"""

    DEFAULT_CSS = """
    ModelSettingsEditor { height: auto; }
    ModelSettingsEditor .model-section { height: auto; }
    ModelSettingsEditor .connection-summary { height: auto; color: $text; border-left: solid $panel; padding-left: 1; margin: 1 0; }
    ModelSettingsEditor .catalog-help { height: auto; color: $text-muted; margin: 1 0 0 0; }
    ModelSettingsEditor Tree { height: auto; min-height: 2; max-height: 24; background: transparent; padding: 0; }
    ModelSettingsEditor .inherit-note { color: $text-muted; height: auto; }
    ModelSettingsEditor .entry-id { color: $text-muted; height: auto; }
    """

    class ViewChanged(Message):
        pass

    class PickRequested(Message):
        def __init__(self, purpose: str) -> None:
            self.purpose = purpose
            super().__init__()

    class DeleteRequested(Message):
        pass

    def __init__(self, show_error=None) -> None:
        super().__init__(id="page-model", classes="settings-page")
        self.config: AppConfig | None = None
        self.current_ref: str | None = None
        self.provider_id: str | None = None
        self.model_id: str | None = None
        self.kind: str | None = None
        self.is_new = False
        self._baseline: dict[str, Any] = {}
        self._show_error = show_error or (lambda _message: None)

    @property
    def editing(self) -> bool:
        return self.kind is not None

    @property
    def dirty(self) -> bool:
        return self.editing and self._values() != self._baseline

    def compose(self) -> ComposeResult:
        with Vertical(id="model-catalog", classes="model-section"):
            with Horizontal(classes="model-usage-row"):
                yield Static("", id="current-model-summary", markup=False)
                yield Button("切换", id="pick-current", classes="link-button")
            with Horizontal(classes="model-usage-row"):
                yield Static("", id="default-model-summary", markup=False)
                yield Button("更改", id="pick-default", classes="link-button")
            yield Static("供应商与模型", id="catalog-heading")
            yield Tree("供应商与模型", id="model-tree")
            yield Button("＋ 添加供应商", id="provider-new", classes="link-button")
            yield Static("供应商保存连接，模型继承参数；Enter 编辑选中项。", classes="catalog-help", id="catalog-help")
        with Vertical(id="provider-fields", classes="model-section"):
            yield Static("", id="provider-title", classes="editor-title", markup=False)
            yield Static("", id="provider-impact", classes="connection-summary", markup=False)
            yield from self._field("供应商名称", Input(id="provider-name"))
            yield from self._field("调用协议", Select(PROTOCOLS, value="openai", allow_blank=False, id="provider-protocol"))
            yield from self._field("Base URL", Input(id="provider-base-url"))
            yield from self._field("API Key", Input(password=True, placeholder="留空保留原值；支持环境变量引用", id="provider-api-key"))
            with Collapsible(title="高级：请求超时与引用", collapsed=True, id="provider-advanced"):
                yield from self._field("超时（秒）", Input(id="provider-timeout", type="number"))
                yield Static("", id="provider-id", classes="entry-id", markup=False)
            yield Button("＋ 为这个供应商添加模型", id="model-new", classes="link-button")
            yield Button("删除供应商…", id="provider-delete", classes="danger-button")
        with Vertical(id="model-fields", classes="model-section"):
            yield Static("", id="model-title", classes="editor-title", markup=False)
            yield Static("", id="model-connection-summary", classes="connection-summary", markup=False)
            yield from self._field("API 模型名", Input(id="model-name"))
            yield from self._field("显示名称（可选）", Input(id="model-display-name"))
            yield Static("保存参数不改变本次模型或新对话默认选择。", classes="inherit-note")
            with Collapsible(title="高级：独立连接与模型参数", collapsed=True, id="model-advanced"):
                for name, label, widget in (
                    ("protocol", "调用协议", Select(PROTOCOLS, value="openai", allow_blank=False, id="model-protocol")),
                    ("base-url", "Base URL", Input(id="model-base-url")),
                    ("api-key", "API Key（留空保留）", Input(password=True, id="model-api-key")),
                    ("timeout", "超时（秒）", Input(id="model-timeout", type="number")),
                ):
                    with Vertical(classes="field"):
                        yield Static(label, classes="field-label")
                        yield Checkbox("继承供应商", value=True, id=f"inherit-{name}", classes="model-inherit")
                        yield widget
                yield from self._field("上下文窗口", Input(id="model-context-window", type="integer"))
                with Vertical(id="model-thinking-fields", classes="model-section"):
                    yield Checkbox("启用 Anthropic thinking", id="model-thinking")
                    yield from self._field("Thinking budget", Input(id="model-thinking-budget", type="integer"))
                yield Static("", id="model-id", classes="entry-id", markup=False)
            yield Button("删除模型", id="model-delete", classes="danger-button")

    @staticmethod
    def _field(label: str, widget: Any) -> ComposeResult:
        with Vertical(classes="field"):
            yield Static(label, classes="field-label")
            yield widget

    def set_compact(self, compact: bool) -> None:
        self.set_class(compact, "-compact")
        self.query_one("#catalog-help", Static).update("Enter 编辑 · ←/→ 展开收起" if compact else "供应商保存连接，模型继承参数；Enter 编辑选中项。")
        # 尺寸变化只更新标签，不重建节点，以保留键盘焦点和展开状态。
        if self.config is not None:
            for node in self.query_one("#model-tree", Tree).root.children:
                _, provider_id, _ = node.data
                provider = self.config.providers[provider_id]
                node.set_label(self._provider_label(provider))
                for child in node.children:
                    _, _, model_id = child.data
                    child.set_label(self._model_label(f"{provider_id}/{model_id}", provider.models[model_id]))

    def load_config(self, config: AppConfig, current_ref: str | None = None) -> None:
        self.config = deepcopy(config)
        self.current_ref = current_ref or config.default_model
        self.show_catalog()

    def show_catalog(self) -> None:
        self.kind = None
        self.is_new = False
        self._show_sections()
        self._refresh_catalog()
        self.post_message(self.ViewChanged())

    def _show_sections(self) -> None:
        self.query_one("#model-catalog").display = self.kind is None
        self.query_one("#provider-fields").display = self.kind == "provider"
        self.query_one("#model-fields").display = self.kind == "model"

    def _refresh_catalog(self) -> None:
        if self.config is None:
            return
        refs = iter_model_refs(self.config.providers)
        colors = theme_palette(self.app.theme)
        current = model_display_name(self.config.providers, self.current_ref) if self.current_ref in refs else "未选择"
        for widget_id, label, value in (
            ("current-model-summary", "本次对话使用  ", current),
            ("default-model-summary", "新对话默认    ", model_display_name(self.config.providers, self.config.default_model)),
        ):
            summary = Text(label, style=colors["muted"], no_wrap=True, overflow="ellipsis")
            summary.append(value, style=colors["text"])
            self.query_one(f"#{widget_id}", Static).update(summary)
        tree = self.query_one("#model-tree", Tree)
        tree.show_root = False
        tree.root.remove_children()
        for provider_id, provider in self.config.providers.items():
            node = tree.root.add(self._provider_label(provider), ("provider", provider_id, None), expand=True)
            for model_id, model in provider.models.items():
                ref = f"{provider_id}/{model_id}"
                node.add_leaf(self._model_label(ref, model), ("model", provider_id, model_id))
        tree.root.expand()

    def _provider_label(self, provider: ProviderDefinition) -> Text:
        label = Text(provider.name, style="bold")
        label.append(f"  {len(provider.models)} 个模型", style="not bold " + theme_palette(self.app.theme)["muted"])
        return label

    def _model_label(self, reference: str, model: ModelDefinition) -> Text:
        compact = self.has_class("-compact")
        secondary = "not bold " + theme_palette(self.app.theme)["muted"]
        label = Text(model.display_name or model.model_name, style="bold")
        if model.display_name and not compact:
            label.append(f"  {model.model_name}", style=secondary)
        flags = []
        if reference == self.current_ref:
            flags.append("本次" if compact else "本次使用")
        if reference == self.config.default_model:
            flags.append("默认" if compact else "新对话默认")
        if flags:
            label.append(f"  [{' · '.join(flags)}]", style=secondary)
        return label

    @on(Tree.NodeSelected, "#model-tree")
    def select_entry(self, event: Tree.NodeSelected) -> None:
        if event.node.data:
            kind, provider_id, model_id = event.node.data
            if kind == "provider":
                self.open_provider(provider_id)
            else:
                self.open_model(provider_id, model_id)

    def open_provider(self, provider_id: str | None = None) -> None:
        self.kind, self.provider_id, self.model_id = "provider", provider_id, None
        self.is_new = provider_id is None
        provider = self.config.providers[provider_id] if provider_id else ProviderDefinition(name="", protocol="openai", base_url="https://api.openai.com/v1", api_key="", models={})
        self._provider_original = deepcopy(provider)
        self.query_one("#provider-title", Static).update("添加供应商" if self.is_new else f"编辑供应商 · {provider.name}")
        names = [m.display_name or m.model_name for m in provider.models.values()]
        self.query_one("#provider-impact", Static).update("保存后可添加第一个模型。" if not names else "以下模型共用连接：" + "、".join(names) + "\n继承参数从下一次请求生效。")
        with self.prevent(Select.Changed, Checkbox.Changed):
            for key, value in {"name":provider.name, "base-url":provider.base_url, "api-key":"", "timeout":str(provider.timeout_seconds)}.items():
                self.query_one(f"#provider-{key}", Input).value = value
            self.query_one("#provider-protocol", Select).value = provider.protocol
        self.query_one("#provider-id", Static).update(f"稳定 ID：{provider_id or '保存时生成'}")
        self.query_one("#model-new").display = not self.is_new
        self.query_one("#provider-delete").display = not self.is_new
        self.query_one("#provider-advanced", Collapsible).collapsed = True
        self._finish_open()

    def open_model(self, provider_id: str, model_id: str | None = None) -> None:
        self.kind, self.provider_id, self.model_id = "model", provider_id, model_id
        self.is_new = model_id is None
        provider = self.config.providers[provider_id]
        model = provider.models[model_id] if model_id else ModelDefinition(model_name="")
        self._model_original = deepcopy(model)
        self.query_one("#model-title", Static).update("添加模型" if self.is_new else f"编辑模型 · {model.display_name or model.model_name}")
        self.query_one("#model-connection-summary", Static).update(f"所属供应商　{provider.name}\n{provider.base_url}\n地址、密钥与超时默认沿用这个供应商。")
        with self.prevent(Select.Changed, Checkbox.Changed):
            self.query_one("#model-name", Input).value = model.model_name
            self.query_one("#model-display-name", Input).value = model.display_name
            for field, attribute in (("protocol", "protocol"), ("base-url", "base_url"), ("api-key", "api_key"), ("timeout", "timeout_seconds")):
                own = getattr(model, attribute)
                self.query_one(f"#inherit-{field}", Checkbox).value = own is None
                self.query_one(f"#model-{field}").value = "" if field == "api-key" else str(own if own is not None else getattr(provider, attribute))
            self.query_one("#model-context-window", Input).value = str(model.context_window or "")
            self.query_one("#model-thinking", Checkbox).value = bool(model.thinking and model.thinking.enabled)
            self.query_one("#model-thinking-budget", Input).value = str(model.thinking.budget_tokens or "") if model.thinking else ""
        self.query_one("#model-id", Static).update(f"稳定引用：{provider_id}/{model_id or '保存时生成'}")
        self.query_one("#model-delete").display = not self.is_new
        self.query_one("#model-advanced", Collapsible).collapsed = False if any(getattr(model, key) is not None for key in ("protocol", "base_url", "api_key", "timeout_seconds")) else True
        self._sync_inheritance()
        self._finish_open()

    def _finish_open(self) -> None:
        self._show_sections()
        self._baseline = self._values()
        self.post_message(self.ViewChanged())
        self.query_one("#provider-name" if self.kind == "provider" else "#model-name", Input).focus()

    def _values(self) -> dict[str, Any]:
        if not self.editing:
            return {}
        area = self.query_one("#provider-fields" if self.kind == "provider" else "#model-fields")
        return {widget.id: widget.value for widget in area.query("Input, Select, Checkbox")}

    @on(Checkbox.Changed, ".model-inherit")
    @on(Select.Changed, "#model-protocol")
    def inheritance_changed(self) -> None:
        self._sync_inheritance()

    def _sync_inheritance(self) -> None:
        if self.kind != "model":
            return
        provider = self.config.providers[self.provider_id]
        with self.prevent(Select.Changed):
            for name, attr in (("protocol", "protocol"), ("base-url", "base_url"), ("api-key", "api_key"), ("timeout", "timeout_seconds")):
                inherited = self.query_one(f"#inherit-{name}", Checkbox).value
                widget = self.query_one(f"#model-{name}")
                widget.disabled = inherited
                if inherited:
                    widget.value = "" if name == "api-key" else str(getattr(provider, attr))
        protocol = self.config.providers[self.provider_id].protocol if self.query_one("#inherit-protocol", Checkbox).value else self.query_one("#model-protocol", Select).value
        self.query_one("#model-thinking-fields").display = protocol == "claude"

    @on(Button.Pressed, "#provider-new")
    def new_provider(self) -> None:
        self.open_provider()

    @on(Button.Pressed, "#model-new")
    def new_model(self) -> None:
        # 由设置页统一处理未保存的供应商草稿。
        self.post_message(self.PickRequested("add-model"))

    @on(Button.Pressed, "#pick-current")
    def pick_current(self) -> None:
        self.post_message(self.PickRequested("current"))

    @on(Button.Pressed, "#pick-default")
    def pick_default(self) -> None:
        self.post_message(self.PickRequested("default"))

    @on(Button.Pressed, "#provider-delete")
    @on(Button.Pressed, "#model-delete")
    def delete_entry(self) -> None:
        self.post_message(self.DeleteRequested())

    def collect(self) -> AppConfig:
        candidate = deepcopy(self.config)
        if self.kind == "provider":
            provider = deepcopy(self._provider_original)
            provider.name = self.query_one("#provider-name", Input).value.strip()
            provider.protocol = self.query_one("#provider-protocol", Select).value
            provider.base_url = self.query_one("#provider-base-url", Input).value.strip()
            provider.api_key = self.query_one("#provider-api-key", Input).value.strip() or provider.api_key
            provider.timeout_seconds = self._positive_number("provider-timeout", "供应商超时")
            provider_id = self.provider_id or new_entry_id(f"provider-{uuid4().hex[:12]}", candidate.providers)
            candidate.providers[provider_id] = provider
        elif self.kind == "model":
            model = deepcopy(self._model_original)
            model.model_name = self.query_one("#model-name", Input).value.strip()
            model.display_name = self.query_one("#model-display-name", Input).value.strip()
            for field, attr in (("protocol", "protocol"), ("base-url", "base_url"), ("api-key", "api_key"), ("timeout", "timeout_seconds")):
                if self.query_one(f"#inherit-{field}", Checkbox).value:
                    value = None
                elif field == "timeout":
                    value = self._positive_number("model-timeout", "模型超时")
                else:
                    value = self.query_one(f"#model-{field}").value.strip()
                    if field == "api-key":
                        value = value or model.api_key or ""
                setattr(model, attr, value)
            model.context_window = self._positive_number("model-context-window", "上下文窗口", integer=True) if self.query_one("#model-context-window", Input).value.strip() else None
            enabled = self.query_one("#model-thinking", Checkbox).value
            budget = self.query_one("#model-thinking-budget", Input).value.strip()
            model.thinking = ThinkingConfig(enabled=enabled, budget_tokens=self._positive_number("model-thinking-budget", "Thinking budget", integer=True) if budget else None) if enabled or budget or model.thinking is not None else None
            models = candidate.providers[self.provider_id].models
            model_id = self.model_id or new_entry_id(f"model-{uuid4().hex[:12]}", models)
            models[model_id] = model
        return candidate

    def _positive_number(self, widget_id: str, label: str, *, integer: bool = False) -> Any:
        try:
            raw = self.query_one(f"#{widget_id}", Input).value.strip()
            value = int(raw) if integer else float(raw)
            if value <= 0 or not math.isfinite(value):
                raise ValueError
            return value
        except ValueError as exc:
            raise ValueError(f"{label}必须是正{'整数' if integer else '数'}。") from exc
