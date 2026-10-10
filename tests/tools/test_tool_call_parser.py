from __future__ import annotations

import pytest

from lancher_code.errors import ToolCallParseError
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.tools.parser import ToolCallAssembler


def test_tool_call_parser_builds_single_call() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="read_file"))
    assembler.consume(ToolCallChunk(call_index=0, arguments_delta='{"path":"demo.txt"}'))

    calls, results = assembler.finalize_batch()

    assert results == []
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

    calls, results = assembler.finalize_batch()

    assert results == []
    assert [call.tool_name for call in calls] == ["read_file", "find_files"]
    assert calls[0].arguments["path"] == "a.txt"
    assert calls[1].arguments["pattern"] == "**/*.py"


def test_tool_call_parser_raises_for_missing_name() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="call-1", arguments_delta='{"path":"demo.txt"}'))

    with pytest.raises(ToolCallParseError):
        assembler.finalize_batch()


@pytest.mark.parametrize("raw", ['{"path":', '["demo.txt"]'])
def test_tool_call_parser_returns_unexecuted_feedback_for_invalid_arguments(raw: str) -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="read_file", arguments_delta=raw))

    calls, results = assembler.finalize_batch()

    assert calls[0].call_id == "call-1"
    assert calls[0].arguments == {"INVALID_JSON": raw}
    assert results[0].call_id == "call-1"
    assert results[0].error_code == "tool_call_parse_error"
    assert results[0].is_error
    assert results[0].metadata["started"] is False


def test_tool_call_parser_accepts_empty_arguments_for_no_argument_tool() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="call-1", name_delta="process_list"))

    calls, results = assembler.finalize_batch()

    assert calls[0].arguments == {}
    assert results == []


def test_malformed_batch_preserves_real_calls_and_does_not_repeat_raw_input() -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="valid", name_delta="read_file",
                                   arguments_delta='{"path":"a.py"}'))
    raw = '{"content":"' + "网页正文" * 500
    assembler.consume(ToolCallChunk(call_index=1, provider_call_id="bad", name_delta="write_file",
                                   arguments_delta=raw))

    calls, results = assembler.finalize_batch()

    assert [call.call_id for call in calls] == ["valid", "bad"]
    assert calls[1].arguments == {"INVALID_JSON": raw}
    assert [result.call_id for result in results] == ["valid", "bad"]
    assert [result.error_code for result in results] == ["tool_batch_not_executed", "tool_call_parse_error"]
    assert all(result.is_error and result.metadata["started"] is False for result in results)
    assert all("分段" in result.content and raw not in result.content for result in results)


@pytest.mark.parametrize("stop_reason", ["length", "max_tokens", "max_output_tokens"])
def test_output_limit_rejects_even_valid_tool_batch(stop_reason) -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, provider_call_id="real", name_delta="write_file",
                                   arguments_delta='{"content":"短正文"}'))
    calls, results = assembler.finalize_batch(stop_reason=stop_reason)
    assert calls[0].arguments == {"content": "短正文"}
    assert results[0].error_code == "tool_batch_not_executed"
    assert "输出上限" in results[0].content


@pytest.mark.parametrize("identity", [{"name_delta": "write_file"}, {"provider_call_id": "real"}])
def test_batch_missing_real_identity_cannot_fabricate_tool_exchange(identity) -> None:
    assembler = ToolCallAssembler()
    assembler.consume(ToolCallChunk(call_index=0, arguments_delta="{}", **identity))
    with pytest.raises(ToolCallParseError, match="真实调用编号或工具名"):
        assembler.finalize_batch()


def test_duplicate_call_ids_stop_before_feedback() -> None:
    assembler = ToolCallAssembler()
    for index in (0, 1):
        assembler.consume(ToolCallChunk(call_index=index, provider_call_id="same", name_delta="read_file",
                                       arguments_delta="{}"))
    with pytest.raises(ToolCallParseError, match="编号重复"):
        assembler.finalize_batch()
