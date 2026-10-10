from __future__ import annotations

from dataclasses import replace

from rich.console import RenderableType
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import Click, Message
from textual.widgets import Static, TextArea

from lancher_code.tui.commands import SlashCompletionCandidate
from lancher_code.tui.theme import theme_palette


class ComposerSubmitted(Message):
    def __init__(self, composer: "ComposerTextArea", value: str, delivery: str | None = None) -> None:
        super().__init__()
        self.composer = composer
        self.value = value
        self.delivery = delivery


class SlashMenuNavigateRequested(Message):
    def __init__(self, direction: int) -> None:
        super().__init__()
        self.direction = direction


class SlashMenuAcceptRequested(Message):
    pass


class SlashMenuDismissRequested(Message):
    pass


class SlashCompletionChosen(Message):
    def __init__(self, candidate_key: str) -> None:
        super().__init__()
        self.candidate_key = candidate_key


class WorkPhaseCycleRequested(Message):
    pass


class StopTurnRequested(Message):
    """输入区的 Esc 停止本轮；弹窗和菜单保留自己的返回行为。"""


class ComposerTextArea(TextArea, inherit_bindings=False):
    BINDINGS = [
        Binding("enter", "submit_message", "发送", show=False, priority=True),
        Binding("tab", "accept_slash_menu_selection", "补全命令", show=False, priority=True),
        Binding("shift+tab", "cycle_work_phase", "切换阶段", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "换行", show=False, priority=True),
        Binding("ctrl+enter", "submit_steering", "补充当前任务", show=False, priority=True),
    ] + [
        replace(binding, key=",".join(key for key in binding.key.split(",") if key != "ctrl+d"))
        for binding in TextArea.BINDINGS
        if any(key != "ctrl+d" for key in binding.key.split(","))
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.slash_menu_active = False
        self.slash_enter_accepts = True
        self._accepted_slash_command_text: str | None = None

    async def _on_key(self, event: events.Key) -> None:
        if self.slash_menu_active:
            if event.key == "up":
                self.post_message(SlashMenuNavigateRequested(-1))
                event.prevent_default()
                return
            if event.key == "down":
                self.post_message(SlashMenuNavigateRequested(1))
                event.prevent_default()
                return
            if event.key == "tab":
                self.post_message(SlashMenuAcceptRequested())
                event.prevent_default()
                return
            if event.key == "escape":
                self.post_message(SlashMenuDismissRequested())
                event.prevent_default()
                event.stop()
                return
        if event.key == "escape":
            self.post_message(StopTurnRequested())
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    def action_submit_message(self) -> None:
        if self.slash_menu_active and self.slash_enter_accepts:
            self.post_message(SlashMenuAcceptRequested())
            return
        self.post_message(ComposerSubmitted(self, self.text))

    def action_accept_slash_menu_selection(self) -> None:
        self.post_message(SlashMenuAcceptRequested())

    def action_cycle_work_phase(self) -> None:
        self.post_message(WorkPhaseCycleRequested())

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_submit_steering(self) -> None:
        self.post_message(ComposerSubmitted(self, self.text, "steer"))

    def remember_accepted_slash_command(self, command_text: str) -> None:
        self._accepted_slash_command_text = command_text

    def should_suppress_slash_menu(self) -> bool:
        return self._accepted_slash_command_text == self.text

    def clear_accepted_slash_command_if_needed(self) -> None:
        if self._accepted_slash_command_text != self.text:
            self._accepted_slash_command_text = None


class SlashCompletionMenuItem(Static):
    def __init__(self, candidate: SlashCompletionCandidate, *, column_width: int = 11) -> None:
        super().__init__(classes="slash-command-item")
        self.candidate = candidate
        self.column_width = column_width
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active
        self.set_class(active, "-active")
        self.refresh()

    def render(self) -> RenderableType:
        colors = theme_palette(self.app.theme)
        foreground = colors["background"] if self._active else colors["text"]
        if self.candidate.presentation == "session":
            # 标题占据主行，身份和状态独立一行；窄终端不让 UUID 把标题挤掉。
            text = Text(no_wrap=False)
            text.append("› " if self._active else "  ", style=foreground)
            title = Text(self.candidate.display, style="bold " + foreground)
            title.truncate(max(1, self.size.width - 2), overflow="ellipsis")
            text.append_text(title)
            text.append("\n  ")
            metadata = self.candidate.description
            if self.size.width < 40:
                parts = metadata.rsplit(" · ", 1)
                # 日期独占元数据末项；窄屏省掉年份，仍保留月日和更新时间。
                metadata = " · ".join((parts[0], parts[1][5:]))
            text.append(metadata, style=foreground if self._active else colors["muted"])
            return text
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("› " if self._active else "  ", style=foreground)
        column = min(self.column_width, max(8, (self.size.width - 4) // 2), 24)
        token = Text(self.candidate.display, style="bold " + foreground)
        token.truncate(column, overflow="ellipsis", pad=True)
        text.append_text(token)
        text.append("  ")
        title, _, detail = self.candidate.description.partition(" · ")
        text.append(title, style=foreground)
        if detail and self.size.width >= 56:
            text.append(" · " + detail, style=foreground if self._active else colors["muted"])
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(SlashCompletionChosen(self.candidate.key))


class SlashCompletionMenu(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="slash-command-menu")

    async def set_candidates(
        self,
        candidates: list[SlashCompletionCandidate],
        active_key: str | None,
    ) -> None:
        self.display = bool(candidates)
        items = list(self.query(SlashCompletionMenuItem))
        if [item.candidate for item in items] != candidates:
            await self.remove_children()
            column_width = max(8, max((cell_len(candidate.display) for candidate in candidates), default=8))
            items = [SlashCompletionMenuItem(candidate, column_width=column_width) for candidate in candidates]
            if items:
                await self.mount(*items)
        if not candidates:
            return
        for item in items:
            item.set_active(item.candidate.key == active_key)
        # 提示文字会改变可用高度，布局完成后再确保选中行可见。
        self.call_after_refresh(self.reveal_active)

    def reveal_active(self) -> None:
        for item in self.query(SlashCompletionMenuItem):
            if item._active:
                self.scroll_to_widget(item, animate=False, immediate=True)
                break


class CommandHintBar(Static):
    def __init__(self) -> None:
        super().__init__("", id="command-hint")
        self.display = False

    def set_hint(self, hint: str) -> None:
        self.update(Text(hint))
        self.display = bool(hint)
