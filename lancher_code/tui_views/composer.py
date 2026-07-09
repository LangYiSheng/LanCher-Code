from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Click, Message
from textual.widgets import Static, TextArea

from lancher_code.slash_commands import SlashCommandDefinition


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


class SlashCommandChosen(Message):
    def __init__(self, command_name: str) -> None:
        super().__init__()
        self.command_name = command_name


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

    def _on_key(self, event: events.Key) -> None:
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
        super()._on_key(event)

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


class SlashCommandMenuItem(Static):
    def __init__(self, definition: SlashCommandDefinition) -> None:
        super().__init__(classes="slash-command-item")
        self.definition = definition
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active
        self.set_class(active, "-active")
        self.refresh()

    def render(self) -> RenderableType:
        text = Text()
        text.append(self.definition.usage, style="bold #73b6ff" if not self._active else "bold #f2f2f2")
        text.append("  ")
        text.append(self.definition.description, style="#a8b9cc" if not self._active else "#dbe7f3")
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(SlashCommandChosen(self.definition.name))


class SlashCommandMenu(Vertical):
    def __init__(self, commands: list[SlashCommandDefinition]) -> None:
        super().__init__(id="slash-command-menu")
        self._commands = commands

    def compose(self) -> ComposeResult:
        for command in self._commands:
            yield SlashCommandMenuItem(command)

    def set_matches(self, commands: list[SlashCommandDefinition], active_name: str | None) -> None:
        visible_names = {command.name for command in commands}
        self.display = bool(commands)
        for item in self.query(SlashCommandMenuItem):
            visible = item.definition.name in visible_names
            item.display = visible
            item.set_active(visible and item.definition.name == active_name)


class CommandHintBar(Static):
    def __init__(self) -> None:
        super().__init__("", id="command-hint")
        self.display = False

    def set_hint(self, hint: str) -> None:
        self.update(hint)
        self.display = bool(hint)
