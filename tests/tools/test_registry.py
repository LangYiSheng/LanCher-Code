from __future__ import annotations

import pytest

from lancher_code.errors import ToolNotFoundError
from lancher_code.tools.builtin.read_file import ReadFileTool
from lancher_code.tools.core.registry import ToolRegistry


def test_registry_registers_and_lists_tools() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool()

    registry.register(tool)

    assert registry.get("read_file") is tool
    assert [definition.name for definition in registry.list_definitions()] == ["read_file"]


def test_registry_rejects_duplicate_registration() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    with pytest.raises(ValueError):
        registry.register(ReadFileTool())


def test_registry_raises_for_missing_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError):
        registry.get("missing")
