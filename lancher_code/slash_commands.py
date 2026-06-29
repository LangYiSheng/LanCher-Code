from __future__ import annotations

from dataclasses import dataclass

from lancher_code.models import RuntimeMode


@dataclass(frozen=True, slots=True)
class SlashCommandDefinition:
    name: str
    description: str
    usage: str
    argument_hint: str = ""
    visible_modes: tuple[RuntimeMode, ...] = ("normal", "plan")
    executable_modes: tuple[RuntimeMode, ...] = ("normal", "plan")
    insert_trailing_space: bool = False

    @property
    def trigger(self) -> str:
        return f"/{self.name}"

    @property
    def insert_text(self) -> str:
        return self.trigger + (" " if self.insert_trailing_space else "")

    @property
    def hint_text(self) -> str:
        if self.argument_hint:
            return f"{self.description}；参数可选：{self.argument_hint}"
        return self.description


@dataclass(frozen=True, slots=True)
class SlashCommandMatch:
    definition: SlashCommandDefinition
    command_text: str
    arguments_text: str = ""

    @property
    def has_arguments(self) -> bool:
        return bool(self.arguments_text.strip())


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommandDefinition] = {}

    def register(self, definition: SlashCommandDefinition) -> None:
        if definition.name in self._commands:
            raise ValueError(f"命令已注册：{definition.name}")
        self._commands[definition.name] = definition

    def list_all(self) -> list[SlashCommandDefinition]:
        return list(self._commands.values())

    def get(self, name: str) -> SlashCommandDefinition | None:
        return self._commands.get(name)

    def visible_commands(self, mode: RuntimeMode) -> list[SlashCommandDefinition]:
        return [command for command in self._commands.values() if mode in command.visible_modes]

    def suggest(self, prefix: str, mode: RuntimeMode) -> list[SlashCommandDefinition]:
        normalized = prefix.casefold()
        return [
            command
            for command in self.visible_commands(mode)
            if command.name.casefold().startswith(normalized)
        ]

    def parse_submission(self, text: str, mode: RuntimeMode) -> SlashCommandMatch | None:
        token = _extract_leading_token(text)
        if token is None or not token.startswith("/"):
            return None

        definition = self.get(token[1:])
        if definition is None or mode not in definition.executable_modes:
            return None

        remainder = text.lstrip()[len(token) :].strip()
        return SlashCommandMatch(
            definition=definition,
            command_text=token,
            arguments_text=remainder,
        )


def extract_slash_menu_query(text: str) -> str | None:
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None

    token = _extract_leading_token(stripped)
    if token is None:
        return None

    if stripped != token:
        return None

    return token[1:]


def extract_exact_command_name(text: str) -> str | None:
    token = _extract_leading_token(text)
    if token is None or not token.startswith("/"):
        return None
    return token[1:]


def create_default_slash_command_registry() -> SlashCommandRegistry:
    registry = SlashCommandRegistry()
    registry.register(
        SlashCommandDefinition(
            name="plan",
            description="继续补充或修改计划",
            usage="/plan [任务]",
            argument_hint="任务描述",
            visible_modes=("normal",),
            executable_modes=("normal", "plan"),
            insert_trailing_space=True,
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="do",
            description="返回正常模式",
            usage="/do",
            visible_modes=("plan",),
            executable_modes=("normal", "plan"),
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="exit",
            description="退出当前会话",
            usage="/exit",
            visible_modes=("normal", "plan"),
            executable_modes=("normal", "plan"),
        )
    )
    return registry


def _extract_leading_token(text: str) -> str | None:
    stripped = text.lstrip()
    if not stripped:
        return None

    token_chars: list[str] = []
    for character in stripped:
        if character.isspace():
            break
        token_chars.append(character)

    if not token_chars:
        return None
    return "".join(token_chars)
