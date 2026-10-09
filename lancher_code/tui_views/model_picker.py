from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from lancher_code.model_catalog import iter_model_refs, model_display_name
from lancher_code.models import AppConfig


class ModelPickerScreen(ModalScreen[str | None]):
    """只选择模型引用，网络请求由下一轮对话发起。"""

    BINDINGS = [
        Binding("escape", "cancel", "取消", priority=True),
        Binding("up", "previous_model", "上一项", show=False, priority=True),
        Binding("down", "next_model", "下一项", show=False, priority=True),
        Binding("enter", "choose_model", "选择", priority=True),
    ]
    CSS = """
    ModelPickerScreen { align: center middle; background: $background 85%; }
    #model-picker { width: 90%; max-width: 88; height: 90%; max-height: 32; padding: 1 2;
        background: $surface; border: solid $panel; color: $text; }
    #model-picker-title { height: 1; text-style: bold; }
    #model-picker-scope { height: auto; margin-bottom: 1; color: $text-muted; }
    #model-search { height: 3; border: none; border-bottom: solid $panel; background: $surface; }
    #model-search:focus { border-bottom: solid $primary; }
    #model-options { height: 1fr; background: transparent; border: none; }
    #model-picker-empty { height: auto; color: $text-muted; display: none; }
    #model-picker-help { height: auto; color: $text-muted; }
    ModelPickerScreen.-narrow #model-picker { width: 100%; height: 100%; padding: 0 1; }
    ModelPickerScreen.-narrow #model-picker-title { height: 1; }
    """

    def __init__(self, config: AppConfig, current_ref: str | None, *, purpose: str = "current") -> None:
        super().__init__()
        self.config = config
        self.current_ref = current_ref
        self.purpose = purpose
        self._visible_refs: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker"):
            yield Static("更改新对话默认模型" if self.purpose == "default" else "切换本次对话模型", id="model-picker-title")
            yield Static("保存后用于新对话；本次对话保持原模型。" if self.purpose == "default" else "立即切换本次对话；新对话默认值保持不变。", id="model-picker-scope")
            yield Input(placeholder="搜索供应商、模型名或显示名称", id="model-search")
            yield OptionList(id="model-options", markup=False)
            yield Static("没有匹配的模型", id="model-picker-empty")
            yield Static("↑↓ 选择 · Enter 确认 · Esc 返回", id="model-picker-help")

    def on_mount(self) -> None:
        self.set_class(self.size.width < 50, "-narrow")
        self._filter_models("")
        self.query_one("#model-search", Input).focus()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 50, "-narrow")

    @on(Input.Changed, "#model-search")
    def search_changed(self, event: Input.Changed) -> None:
        self._filter_models(event.value)

    def _filter_models(self, query: str) -> None:
        words = query.casefold().split()
        options: list[Option] = []
        refs: list[str] = []
        for ref in iter_model_refs(self.config):
            provider_id, model_id = ref.split("/", 1)
            provider = self.config.providers[provider_id]
            model = provider.models[model_id]
            label = model_display_name(self.config, ref)
            searchable = f"{ref} {label} {provider.name} {model.model_name}".casefold()
            if not all(word in searchable for word in words):
                continue
            flags = []
            if ref == self.current_ref:
                flags.append("本次对话")
            if ref == self.config.default_model:
                flags.append("新对话默认")
            suffix = "  · " + " / ".join(flags) if flags else ""
            prompt = Text(label + suffix, style="bold" if ref == self.current_ref else "")
            prompt.append(f"\n{provider.name} · {model.model_name} · {ref}", style="dim")
            options.append(Option(prompt, id=ref))
            refs.append(ref)
        self._visible_refs = refs
        widget = self.query_one("#model-options", OptionList)
        widget.clear_options()
        widget.add_options(options)
        selected = self.config.default_model if self.purpose == "default" else self.current_ref
        widget.highlighted = refs.index(selected) if selected in refs else (0 if refs else None)
        self.query_one("#model-picker-empty", Static).display = not refs

    def _move(self, delta: int) -> None:
        if self._visible_refs:
            widget = self.query_one("#model-options", OptionList)
            widget.highlighted = ((widget.highlighted or 0) + delta) % len(self._visible_refs)

    def action_previous_model(self) -> None:
        self._move(-1)

    def action_next_model(self) -> None:
        self._move(1)

    def action_choose_model(self) -> None:
        index = self.query_one("#model-options", OptionList).highlighted
        if index is not None and index < len(self._visible_refs):
            self.dismiss(self._visible_refs[index])

    @on(OptionList.OptionSelected, "#model-options")
    def option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id in self._visible_refs:
            self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
