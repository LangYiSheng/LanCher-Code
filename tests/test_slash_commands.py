from __future__ import annotations

from lancher_code.slash_commands import (
    create_default_slash_command_registry,
    extract_exact_command_name,
    extract_slash_menu_query,
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

    assert [command.name for command in registry.suggest("", "default")] == ["plan", "mode", "settings", "exit"]
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
