from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import Click, Message
from textual.widgets import Static, TextArea

from lancher_code.slash_commands import SlashCompletionCandidate


class ComposerSubmitted(Message):
    def __init__(self, composer: "ComposerTextArea", value: str) -> None:
        super().__init__()
        self.composer = composer
        self.value = value


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


class PermissionModeCycleRequested(Message):
    pass


class ComposerTextArea(TextArea):
    BINDINGS = [
        Binding("enter", "submit_message", "发送", show=False, priority=True),
        Binding("tab", "accept_slash_menu_selection", "补全命令", show=False, priority=True),
        Binding("shift+tab", "cycle_permission_mode", "切换模式", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "换行", show=False, priority=True),
    ] + TextArea.BINDINGS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.slash_menu_active = False
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
                return
        await super()._on_key(event)

    def action_submit_message(self) -> None:
        if self.slash_menu_active:
            self.post_message(SlashMenuAcceptRequested())
            return
        self.post_message(ComposerSubmitted(self, self.text))

    def action_accept_slash_menu_selection(self) -> None:
        if self.slash_menu_active:
            self.post_message(SlashMenuAcceptRequested())

    def action_cycle_permission_mode(self) -> None:
        self.post_message(PermissionModeCycleRequested())

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def remember_accepted_slash_command(self, command_text: str) -> None:
        self._accepted_slash_command_text = command_text

    def should_suppress_slash_menu(self) -> bool:
        return self._accepted_slash_command_text == self.text

    def clear_accepted_slash_command_if_needed(self) -> None:
        if self._accepted_slash_command_text != self.text:
            self._accepted_slash_command_text = None


class SlashCompletionMenuItem(Static):
    def __init__(self, candidate: SlashCompletionCandidate) -> None:
        super().__init__(classes="slash-command-item")
        self.candidate = candidate
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active
        self.set_class(active, "-active")
        self.refresh()

    def render(self) -> RenderableType:
        text = Text()
        text.append(self.candidate.display, style="bold #73b6ff" if not self._active else "bold #f2f2f2")
        text.append("  ")
        text.append(self.candidate.description, style="#a8b9cc" if not self._active else "#dbe7f3")
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
        await self.remove_children()
        self.display = bool(candidates)
        if not candidates:
            return
        items = [SlashCompletionMenuItem(candidate) for candidate in candidates]
        await self.mount(*items)
        for item in items:
            item.set_active(item.candidate.key == active_key)
            if item.candidate.key == active_key:
                item.scroll_visible(animate=False)


# 保留旧导出名，避免外部调用方因菜单泛化而立即失效。
SlashCommandChosen = SlashCompletionChosen
SlashCommandMenuItem = SlashCompletionMenuItem
SlashCommandMenu = SlashCompletionMenu


class CommandHintBar(Static):
    def __init__(self) -> None:
        super().__init__("", id="command-hint")
        self.display = False

    def set_hint(self, hint: str) -> None:
        self.update(hint)
        self.display = bool(hint)
