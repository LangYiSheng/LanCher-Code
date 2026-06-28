from __future__ import annotations

import pytest

from lancher_code.errors import ToolCallParseError
from lancher_code.models import ToolCallChunk
from lancher_code.tool_call_parser import ToolCallAssembler


def test_tool_call_parser_builds_single_call() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="read_file"))
    assembler.consume(ToolCallChunk(call_index=0, arguments_delta='{"path":"demo.txt"}'))

    calls = assembler.finalize()

    assert len(calls) == 1
    assert calls[0].call_id == "call-1"
    assert calls[0].tool_name == "read_file"
    assert calls[0].arguments == {"path": "demo.txt"}


def test_tool_call_parser_builds_multiple_calls_with_chunked_arguments() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="read_file"))
    assembler.consume(ToolCallChunk(call_index=0, arguments_delta='{"path":"'))
    assembler.consume(ToolCallChunk(call_index=0, arguments_delta='a.txt"}'))
    assembler.consume(ToolCallChunk(call_index=1, provider_call_id="call-2", name_delta="find_files"))
    assembler.consume(ToolCallChunk(call_index=1, arguments_delta='{"pattern":"**/*.py"}'))

    calls = assembler.finalize()

    assert [call.tool_name for call in calls] == ["read_file", "find_files"]
    assert calls[0].arguments["path"] == "a.txt"
    assert calls[1].arguments["pattern"] == "**/*.py"


def test_tool_call_parser_raises_for_missing_name() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, arguments_delta='{"path":"demo.txt"}'))

    with pytest.raises(ToolCallParseError):
        assembler.finalize()


def test_tool_call_parser_raises_for_invalid_json() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, name_delta="read_file", arguments_delta='{"path":'))

    with pytest.raises(ToolCallParseError):
        assembler.finalize()


def test_tool_call_parser_raises_when_arguments_are_not_object() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, name_delta="read_file", arguments_delta='["demo.txt"]'))

    with pytest.raises(ToolCallParseError):
        assembler.finalize()
