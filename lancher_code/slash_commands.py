from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lancher_code.models import RuntimeMode


@dataclass(frozen=True, slots=True)
class SlashCompletionContext:
    text: str
    mode: RuntimeMode
    session_names: tuple[str, ...] = ()
    active_session_name: str | None = None


@dataclass(frozen=True, slots=True)
class SlashArgumentSuggestion:
    value: str
    description: str
    append_space: bool = False


@dataclass(frozen=True, slots=True)
class SlashCompletionCandidate:
    key: str
    value: str
    display: str
    description: str
    replace_start: int
    replace_end: int
    append_space: bool = False

    def apply(self, text: str) -> str:
        suffix = " " if self.append_space else ""
        return f"{text[:self.replace_start]}{self.value}{suffix}{text[self.replace_end:]}"


SlashArgumentCompleter = Callable[
    [tuple[str, ...], str, SlashCompletionContext],
    list[SlashArgumentSuggestion],
]


@dataclass(frozen=True, slots=True)
class SlashCommandDefinition:
    name: str
    description: str
    usage: str
    argument_hint: str = ""
    visible_modes: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass")
    executable_modes: tuple[RuntimeMode, ...] = ("default", "plan", "acceptEdits", "bypass")
    insert_trailing_space: bool = False
    argument_completer: SlashArgumentCompleter | None = None

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

    def complete(self, context: SlashCompletionContext) -> list[SlashCompletionCandidate]:
        text = context.text
        if not text or "\n" in text or "\r" in text:
            return []
        stripped = text.lstrip()
        leading = len(text) - len(stripped)
        if not stripped.startswith("/"):
            return []

        if not any(character.isspace() for character in stripped):
            prefix = stripped[1:]
            start = leading + 1
            return [
                SlashCompletionCandidate(
                    key=f"command:{command.name}",
                    value=command.name,
                    display=command.usage,
                    description=command.description,
                    replace_start=start,
                    replace_end=len(text),
                    append_space=command.insert_trailing_space,
                )
                for command in self.suggest(prefix, context.mode)
            ]

        tokens = stripped.split()
        if not tokens:
            return []
        command = self.get(tokens[0][1:])
        if (
            command is None
            or context.mode not in command.visible_modes
            or command.argument_completer is None
        ):
            return []

        trailing_space = bool(text and text[-1].isspace())
        arguments = tokens[1:]
        if trailing_space:
            completed = tuple(arguments)
            prefix = ""
            replace_start = len(text)
        else:
            completed = tuple(arguments[:-1])
            prefix = arguments[-1] if arguments else ""
            replace_start = len(text) - len(prefix)

        suggestions = command.argument_completer(completed, prefix, context)
        normalized_prefix = prefix.casefold()
        return [
            SlashCompletionCandidate(
                key=f"argument:{command.name}:{replace_start}:{suggestion.value}",
                value=suggestion.value,
                display=suggestion.value,
                description=suggestion.description,
                replace_start=replace_start,
                replace_end=len(text),
                append_space=suggestion.append_space,
            )
            for suggestion in suggestions
            if suggestion.value.casefold().startswith(normalized_prefix)
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
            visible_modes=("default", "acceptEdits", "bypass"),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
            insert_trailing_space=True,
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="do",
            description="回到进入 plan 前的模式",
            usage="/do",
            visible_modes=("plan",),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="mode",
            description="切换权限模式",
            usage="/mode <default|plan|acceptEdits|bypass>",
            argument_hint="default | plan | acceptEdits | bypass",
            visible_modes=("default", "plan", "acceptEdits", "bypass"),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
            insert_trailing_space=True,
            argument_completer=_complete_mode_arguments,
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="session",
            description="管理项目会话",
            usage="/session <list|save|remove|rename|resume> [名称]",
            argument_hint="list | save 名称 | remove 名称 | rename 旧名称 新名称 | resume 名称 [--force]",
            visible_modes=("default", "plan", "acceptEdits", "bypass"),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
            insert_trailing_space=True,
            argument_completer=_complete_session_arguments,
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="compact",
            description="压缩当前会话上下文",
            usage="/compact",
            visible_modes=("default", "plan", "acceptEdits", "bypass"),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="settings",
            description="打开设置面板",
            usage="/settings",
            visible_modes=("default", "plan", "acceptEdits", "bypass"),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
        )
    )
    registry.register(
        SlashCommandDefinition(
            name="exit",
            description="退出当前会话",
            usage="/exit",
            visible_modes=("default", "plan", "acceptEdits", "bypass"),
            executable_modes=("default", "plan", "acceptEdits", "bypass"),
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


def _complete_mode_arguments(
    completed: tuple[str, ...],
    _prefix: str,
    _context: SlashCompletionContext,
) -> list[SlashArgumentSuggestion]:
    if completed:
        return []
    return [
        SlashArgumentSuggestion(value=mode, description=f"切换到 {mode} 模式")
        for mode in ("default", "plan", "acceptEdits", "bypass")
    ]


def _complete_session_arguments(
    completed: tuple[str, ...],
    _prefix: str,
    context: SlashCompletionContext,
) -> list[SlashArgumentSuggestion]:
    if not completed:
        return [
            SlashArgumentSuggestion("list", "列出项目会话"),
            SlashArgumentSuggestion("save", "保存当前会话", append_space=True),
            SlashArgumentSuggestion("remove", "删除会话", append_space=True),
            SlashArgumentSuggestion("rename", "重命名会话", append_space=True),
            SlashArgumentSuggestion("resume", "恢复会话", append_space=True),
        ]

    action = completed[0]
    if len(completed) == 1 and action in {"resume", "remove", "rename"}:
        names = context.session_names
        if action == "remove":
            names = tuple(name for name in names if name != context.active_session_name)
        return [
            SlashArgumentSuggestion(
                name,
                "已保存的项目会话",
                append_space=action == "rename",
            )
            for name in names
        ]

    if len(completed) == 2 and action == "resume" and completed[1] in context.session_names:
        return [SlashArgumentSuggestion("--force", "丢弃当前未保存改动并恢复")]
    return []
