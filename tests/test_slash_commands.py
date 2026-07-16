from __future__ import annotations

from lancher_code.slash_commands import (
    SlashCompletionContext,
    create_default_slash_command_registry,
    extract_exact_command_name,
    extract_slash_menu_query,
)


def _complete(text: str, *, sessions: tuple[str, ...] = (), active: str | None = None):
    return create_default_slash_command_registry().complete(
        SlashCompletionContext(
            text=text,
            mode="default",
            session_names=sessions,
            active_session_name=active,
        )
    )


def test_extract_slash_menu_query_only_when_first_token_is_being_typed() -> None:
    assert extract_slash_menu_query("/") == ""
    assert extract_slash_menu_query("/pl") == "pl"
    assert extract_slash_menu_query("   /pl") == "pl"
    assert extract_slash_menu_query("/plan 写计划") is None
    assert extract_slash_menu_query("先说一句 /plan") is None


def test_extract_exact_command_name_can_keep_argument_hint_state() -> None:
    assert extract_exact_command_name("/plan") == "plan"
    assert extract_exact_command_name("/plan 写计划") == "plan"
    assert extract_exact_command_name("  /exit") == "exit"
    assert extract_exact_command_name("hello /exit") is None


def test_registry_suggests_commands_by_mode() -> None:
    registry = create_default_slash_command_registry()

    assert [command.name for command in registry.suggest("", "default")] == [
        "plan",
        "mode",
        "session",
        "settings",
        "exit",
    ]
    assert [command.name for command in registry.suggest("d", "default")] == []
    assert [command.name for command in registry.suggest("d", "plan")] == ["do"]


def test_registry_parses_submission_and_keeps_payload() -> None:
    registry = create_default_slash_command_registry()

    match = registry.parse_submission("/plan 为搜索写计划", "default")

    assert match is not None
    assert match.definition.name == "plan"
    assert match.arguments_text == "为搜索写计划"


def test_registry_ignores_unknown_commands() -> None:
    registry = create_default_slash_command_registry()

    assert registry.parse_submission("/unknown", "default") is None


def test_registry_completes_session_subcommands_and_partial_token() -> None:
    candidates = _complete("/session ")
    assert [candidate.value for candidate in candidates] == [
        "list",
        "save",
        "remove",
        "rename",
        "resume",
    ]

    resume = _complete("/session res")[0]
    assert resume.value == "resume"
    assert resume.apply("/session res") == "/session resume "


def test_registry_completes_dynamic_session_names_and_force() -> None:
    sessions = ("最近会话", "older")
    assert [item.value for item in _complete("/session resume ", sessions=sessions)] == list(sessions)
    assert [item.value for item in _complete("/session remove ", sessions=sessions, active="最近会话")] == [
        "older"
    ]
    renamed = _complete("/session rename old", sessions=sessions)[0]
    assert renamed.value == "older"
    assert renamed.apply("/session rename old") == "/session rename older "
    assert [item.value for item in _complete("/session resume older ", sessions=sessions)] == [
        "--force"
    ]


def test_registry_completes_mode_and_ignores_multiline_input() -> None:
    assert [item.value for item in _complete("/mode p")] == ["plan"]
    assert _complete("/session\nresume") == []
