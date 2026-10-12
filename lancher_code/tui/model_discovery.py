"""发现模型后的多选界面，共用于设置与首次配置。"""
from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, SelectionList, Static
from textual.widgets.selection_list import Selection

from lancher_code.providers.discovery import ModelDiscoveryResult


class ModelDiscoveryScreen(ModalScreen[list[str] | None]):
    BINDINGS = [('escape', 'cancel', '取消')]
    CSS = '''
    ModelDiscoveryScreen { align: center middle; background: $background 80%; }
    #model-discovery-box { width: 90; max-width: 96%; height: 90%; min-height: 12; padding: 0 1; background: $surface; border: solid $panel; }
    #model-discovery-title { height: 1; text-style: bold; }
    #model-discovery-summary, #model-discovery-notice { height: auto; color: $text-muted; }
    #model-discovery-search { height: 3; border: none; border-bottom: solid $panel; background: transparent; }
    #model-discovery-options { height: 1fr; min-height: 2; background: transparent; border: none; }
    #model-discovery-details { height: auto; max-height: 4; color: $text-muted; }
    #model-discovery-toggles, #model-discovery-actions { height: 2; }
    #model-discovery-box Button { height: 2; min-width: 6; width: auto; margin-right: 1; border: none; background: transparent; color: $text; }
    #model-discovery-box Button:focus { background: $foreground; color: $background; }
    #model-discovery-add { color: $primary; }
    ModelDiscoveryScreen.-narrow #model-discovery-details, ModelDiscoveryScreen.-narrow #model-discovery-notice { display: none; }
    '''

    def __init__(self, result: ModelDiscoveryResult, existing: set[str] | None = None,
                 *, provider_name: str = '', initial: set[str] | None = None) -> None:
        super().__init__()
        self.result = result
        self._records = {model.id: model for model in result.models}
        self._existing = (existing or set()) & self._records.keys()
        self._eligible = self._records.keys() - self._existing
        self._selected = (initial or set()) & self._eligible
        self._visible: set[str] = set()
        self._provider_name = provider_name

    def compose(self) -> ComposeResult:
        with Vertical(id='model-discovery-box'):
            yield Static(f'发现模型 · {self._provider_name}', id='model-discovery-title', markup=False)
            yield Static('', id='model-discovery-summary', markup=False)
            yield Static(self.result.notice or '空格勾选；已加入的模型保留设置并更新信息。', id='model-discovery-notice', markup=False)
            yield Input(placeholder='搜索名称或归属', id='model-discovery-search')
            with Horizontal(id='model-discovery-toggles'):
                yield Button('全选', id='model-discovery-all')
                yield Button('清空', id='model-discovery-clear')
            yield SelectionList[str](id='model-discovery-options')
            yield Static('', id='model-discovery-details', markup=False)
            with Horizontal(id='model-discovery-actions'):
                yield Button('取消', id='model-discovery-cancel')
                yield Button('加入所选', id='model-discovery-add')

    def on_mount(self) -> None:
        self.set_class(self.size.width < 60, '-narrow')
        self._render_options()
        self.query_one('#model-discovery-options').focus()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 60, '-narrow')

    @on(Input.Changed, '#model-discovery-search')
    def search_changed(self) -> None:
        self._render_options()

    def _render_options(self) -> None:
        query = self.query_one('#model-discovery-search', Input).value.strip().casefold()
        options = []
        for model in self.result.models:
            if query and query not in f'{model.id} {model.display_name} {model.owned_by}'.casefold():
                continue
            label = Text(model.display_name or model.id)
            if model.display_name and model.display_name != model.id:
                label.append(f'  {model.id}', style='dim')
            if model.owned_by:
                label.append(f'  {model.owned_by}', style='dim')
            if model.id in self._existing:
                label.append('  已加入', style='dim')
            options.append(Selection(label, model.id, model.id in self._selected, disabled=model.id in self._existing))
        self._visible = {option.value for option in options}
        widget = self.query_one('#model-discovery-options', SelectionList)
        with self.prevent(SelectionList.SelectedChanged):
            widget.clear_options()
            widget.add_options(options)
        self._refresh_summary()

    @on(SelectionList.SelectedChanged, '#model-discovery-options')
    def selection_changed(self) -> None:
        selected = set(self.query_one('#model-discovery-options', SelectionList).selected)
        self._selected = (self._selected - self._visible) | (selected & self._eligible)
        self._refresh_summary()

    @on(SelectionList.SelectionHighlighted, '#model-discovery-options')
    def highlight_changed(self, event: SelectionList.SelectionHighlighted) -> None:
        model = self._records[event.selection.value]
        cap = model.capabilities
        effort = '未声明' if cap.effort_levels is None else ' / '.join(cap.effort_levels) or '不提供档位'
        limits = f'窗口 {model.context_window or "未声明"} · 输出上限 {model.max_output_tokens or "未声明"}'
        self.query_one('#model-discovery-details', Static).update(
            f'{model.id}\n归属 {model.owned_by or "未声明"} · 推理档位 {effort}\n{limits}\n获取时间 {model.fetched_at}')

    def _refresh_summary(self) -> None:
        source = '上次结果' if self.result.stale else '缓存' if self.result.cached else '在线'
        self.query_one('#model-discovery-summary', Static).update(
            f'{len(self._records)} 个模型 · {len(self._existing)} 已加入 · {len(self._selected)} 已选 · {source}')
        button = self.query_one('#model-discovery-add', Button)
        button.label = f'加入所选（{len(self._selected)}）' if self._selected else '更新已有模型信息'
        button.disabled = not self._selected and not self._existing

    @on(Button.Pressed, '#model-discovery-all')
    def select_all(self) -> None:
        self._selected = set(self._eligible)
        self._render_options()

    @on(Button.Pressed, '#model-discovery-clear')
    def clear_all(self) -> None:
        self._selected.clear()
        self._render_options()

    @on(Button.Pressed, '#model-discovery-cancel')
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, '#model-discovery-add')
    def add_selected(self) -> None:
        self.dismiss([model.id for model in self.result.models if model.id in self._selected])
