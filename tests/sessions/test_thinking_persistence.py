from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import pytest
from provider_helpers import complete_test_response

from lancher_code.context.compaction import compact_transcript, select_recent_history
from lancher_code.context.models import ContextManagementState
from lancher_code.context.projection import project_historical_tool_exchanges
from lancher_code.context.summary import SUMMARY_HEADINGS
from lancher_code.context.tokens import (
    estimate_messages_tokens,
    estimate_request,
    estimate_text_tokens,
    update_usage_anchor,
)
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage, StreamEvent
from lancher_code.contracts.tools import ToolExecutionResult
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.controller import SessionController
from lancher_code.usage.models import MessageUsage


def _thinking_exchange() -> list[ContentBlock]:
    return [
        ContentBlock.thinking_block("先读取文件再执行。", signature="provider-signature"),
        ContentBlock.redacted_thinking_block("opaque-provider-data"),
        ContentBlock.text_block("现在读取文件。"),
        ContentBlock.tool_use_block(call_id="read-1", name="read_file", input={"path": "demo.py"}),
    ]


def test_assistant_exchange_is_deep_copied_and_resumes_without_duplicate_final_text(
    claude_provider_config, tmp_path,
) -> None:
    session = SessionController(claude_provider_config, cwd=tmp_path)
    original_blocks = _thinking_exchange()
    expected_blocks = deepcopy(original_blocks)
    final_blocks = [ContentBlock.thinking_block("内容已确认。", signature="final-signature"),
                    ContentBlock.text_block("文件已读取。")]
    try:
        session.create_user_message("检查文件")
        message = session.create_assistant_message()
        session.append_message_content(message.id, "现在读取文件。文件已读取。")
        session.append_assistant_response(original_blocks)
        original_blocks[0].text = "随后修改了缓冲"
        original_blocks[0].signature = "changed-signature"
        original_blocks[-1].input["path"] = "changed.py"
        original_blocks.clear()
        assert session.transcript[-1].blocks == expected_blocks
        session.append_tool_results([ToolExecutionResult(
            call_id="read-1", tool_name="read_file", content="print('hello')", is_error=False,
        )])
        session.append_assistant_response(final_blocks)
        session.complete_message(message.id, record_transcript=False)
        expected = deepcopy(session.transcript)
        assert [item.role for item in expected] == ["user", "assistant", "tool", "assistant"]
        assert expected[-1].blocks == final_blocks
        assert expected[-1].response_protocol == "claude"
        assert expected[-1].response_model == claude_provider_config.model
        session_id = session.session_id
    finally:
        session.close()

    restored = SessionController(claude_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        assert restored.transcript == expected
        assistant = next(item for item in restored.build_request([], allow_tool_calls=False).messages
                         if item.role == "assistant")
        assert assistant.blocks == expected_blocks
    finally:
        restored.close()


def test_complete_message_default_still_records_plain_assistant_text(openai_provider_config, tmp_path) -> None:
    session = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        session.create_user_message("问题")
        message = session.create_assistant_message()
        session.append_message_content(message.id, "回答")
        session.complete_message(message.id)
        assert session.transcript[-1].blocks == [ContentBlock.text_block("回答")]
        assert session.transcript[-1].response_model == openai_provider_config.model
    finally:
        session.close()


@pytest.mark.parametrize("invalid_block", [
    ContentBlock.thinking_block(""), ContentBlock.redacted_thinking_block(""),
])
def test_invalid_complete_snapshot_never_changes_transcript_or_writes_events(
    claude_provider_config, tmp_path, invalid_block,
) -> None:
    session = SessionController(claude_provider_config, cwd=tmp_path)
    try:
        session.create_user_message("已有原始历史")
        original = deepcopy(session.transcript)
        events = session.paths.events.read_bytes()
        with pytest.raises(ValueError):
            session.append_assistant_response([invalid_block, ContentBlock.text_block("看起来正常的正文")])
        assert session.transcript == original
        assert session.paths.events.read_bytes() == events
    finally:
        session.close()


@pytest.mark.parametrize("block", [
    ContentBlock.thinking_block("可读思考", signature="signature"),
    ContentBlock.thinking_block("", signature="signature-without-visible-text"),
    ContentBlock.redacted_thinking_block("opaque-data"),
    ContentBlock.thinking_block("reasoning", protocol="openai", thinking_field="reasoning_content"),
    ContentBlock.thinking_block("reasoning", protocol="openai", thinking_field="reasoning"),
])
def test_thinking_blocks_round_trip_without_tool_call_identifiers(block) -> None:
    message = ConversationMessage(role="assistant", blocks=[block], response_protocol="claude", response_model="test-model")
    assert SessionCodec.decode_transcript(asdict(message)) == message


def test_protocol_history_without_response_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="缺少完整来源"):
        SessionCodec.decode_transcript({"role": "assistant", "blocks": [
            {"kind": "text", "text": "旧回复"},
            {"kind": "tool_use", "call_id": "old-call", "name": "read_file", "input": {}},
        ]})


@pytest.mark.parametrize("field,value", [
    ("response_protocol", "unknown"), ("response_protocol", []), ("response_model", ""),
    ("response_model", True),
])
def test_invalid_response_provenance_is_rejected(field, value) -> None:
    with pytest.raises(ValueError, match="来源"):
        SessionCodec.decode_transcript({"role": "assistant", "blocks": [], field: value})


@pytest.mark.parametrize("block", [
    {"kind": "thinking", "text": ""},
    {"kind": "thinking", "text": "visible", "signature": 123},
    {"kind": "thinking", "text": "visible", "data": "redacted"},
    {"kind": "thinking", "text": "visible", "thinking_protocol": "unknown"},
    {"kind": "thinking", "text": "visible", "thinking_protocol": []},
    {"kind": "thinking", "text": "visible", "thinking_field": "unknown"},
    {"kind": "thinking", "text": "visible", "thinking_protocol": "claude", "thinking_field": "reasoning"},
    {"kind": "redacted_thinking", "data": ""},
    {"kind": "redacted_thinking", "data": None},
    {"kind": "redacted_thinking", "data": 123},
    {"kind": "redacted_thinking", "data": "opaque", "text": "invented"},
    {"kind": "redacted_thinking", "data": "opaque", "signature": "invented"},
    {"kind": "text", "text": "reply", "signature": "invented"},
    {"kind": "tool_use", "name": "read_file", "input": {}},
    {"kind": "tool_result", "text": "result"},
])
def test_invalid_thinking_or_tool_fields_are_rejected(block) -> None:
    with pytest.raises(ValueError):
        SessionCodec.decode_transcript({"role": "assistant", "blocks": [block],
                                        "response_protocol": "claude", "response_model": "test-model"})


@pytest.mark.parametrize("role", ["user", "system", "tool"])
def test_thinking_is_restricted_to_assistant_messages(role) -> None:
    with pytest.raises(ValueError, match="只能属于助手"):
        SessionCodec.decode_transcript({"role": role, "blocks": [asdict(ContentBlock.thinking_block("reasoning"))]})


def _request_with_thinking() -> ChatRequest:
    return ChatRequest(model="test", messages=[ConversationMessage(
        role="assistant", blocks=[
            ContentBlock.thinking_block("reasoning", signature="signature"),
            ContentBlock.redacted_thinking_block("opaque-data"),
        ],
    )])


def test_thinking_and_opaque_metadata_have_separate_conservative_estimates() -> None:
    request = _request_with_thinking()
    estimate = estimate_request(request, ContextManagementState())
    assert estimate.breakdown["thinking"] == estimate_text_tokens("reasoning")
    assert estimate.breakdown["thinking_metadata"] == estimate_text_tokens("signature") + estimate_text_tokens("opaque-data")
    assert estimate.breakdown["tool_results"] == 0
    assert estimate.breakdown["message_text"] == 0
    assert estimate.tokens == sum(estimate.breakdown.values())


@pytest.mark.parametrize("block_index,field,value", [
    (0, "text", "different reasoning"),
    (0, "signature", "different-signature"),
    (0, "thinking_protocol", "openai"),
    (0, "thinking_field", "reasoning_content"),
    (1, "data", "different-opaque-data"),
])
def test_any_thinking_protocol_content_change_invalidates_usage_anchor(block_index, field, value) -> None:
    request = _request_with_thinking()
    state = ContextManagementState()
    assert update_usage_anchor(state, request, MessageUsage(input_tokens=12_345))
    assert estimate_request(request, state).tokens == 12_345
    setattr(request.messages[0].blocks[block_index], field, value)
    assert estimate_request(request, state).source == "estimated"
    assert state.usage_anchor is None


def test_reported_usage_remains_authoritative_with_thinking_and_added_content() -> None:
    request = _request_with_thinking()
    state = ContextManagementState()
    assert update_usage_anchor(state, request, MessageUsage(input_tokens=321))
    assert estimate_request(request, state).tokens == 321
    request.messages.append(ConversationMessage.text_message("user", "next"))
    estimate = estimate_request(request, state)
    assert estimate.source == "usage_calibrated"
    assert estimate.breakdown["reported_input"] == 321
    assert estimate.tokens == 321 + estimate_text_tokens("next") + 10


def _recent_thinking_tool_group() -> list[ConversationMessage]:
    return [
        ConversationMessage.text_message("user", "当前任务"),
        ConversationMessage(role="assistant", blocks=_thinking_exchange()),
        ConversationMessage(role="tool", blocks=[ContentBlock.tool_result_block(
            call_id="read-1", text="文件内容", is_error=False,
        )]),
        ConversationMessage(role="assistant", blocks=[ContentBlock.thinking_block(
            "已检查", signature="final-signature",
        ), ContentBlock.text_block("检查结束")]),
    ]


def test_recent_tool_group_retains_thinking_signatures_and_redacted_data() -> None:
    group = _recent_thinking_tool_group()
    original = [ConversationMessage.text_message("user", "旧材料" + "x" * 40_000),
                ConversationMessage.text_message("assistant", "旧工作完成"), *group]
    recent = select_recent_history(original, token_budget=20_000)
    assert recent[-len(group):] == group


@pytest.mark.asyncio
async def test_compaction_retains_complete_recent_thinking_tool_exchange() -> None:
    class SummaryProvider:
        def __init__(self):
            self.requests = []

        @complete_test_response
        async def stream_chat(self, request):
            self.requests.append(request)
            yield StreamEvent(kind="text_delta", text="<summary>" + "\n".join(
                f"## {heading}\n已归档旧材料。" for heading in SUMMARY_HEADINGS
            ) + "</summary>")
            yield StreamEvent(kind="message_end", response_complete=True)

    group = _recent_thinking_tool_group()
    original = [ConversationMessage.text_message("user", "旧材料" + "x" * 40_000),
                ConversationMessage.text_message("assistant", "旧工作完成"), *group]
    provider = SummaryProvider()
    result = await compact_transcript(provider=provider, model="test", transcript=original,
                                      visible_tools=[], state=ContextManagementState(), context_window=128000)
    assert len(provider.requests) == 1
    assert estimate_messages_tokens(result.transcript) < estimate_messages_tokens(original)
    assert result.transcript[0].blocks[0].text == "以下内容是较早会话的压缩历史。"
    assert result.transcript[-len(group):] == group


def _plain_tool_exchange(call_id="historical-call", *, protocol=None, model=None):
    return [ConversationMessage(
        role="assistant", blocks=[ContentBlock.text_block("读取过文件。"), ContentBlock.tool_use_block(
            call_id=call_id, name="read_file", input={"path": "demo.py"},
        )], response_protocol=protocol, response_model=model,
    ), ConversationMessage(role="tool", blocks=[ContentBlock.tool_result_block(
        call_id=call_id, text="旧文件原文", is_error=False,
    )])]


def _project(messages, *, protocol="claude", model="claude-current"):
    return project_historical_tool_exchanges(messages, protocol=protocol, model=model)


@pytest.mark.parametrize("protocol,model", [("openai", "other"), ("claude", "claude-older")])
def test_foreign_protocol_or_model_exchange_is_projected(protocol, model) -> None:
    original = _plain_tool_exchange(protocol=protocol, model=model)
    original[0].blocks.insert(0, ContentBlock.thinking_block("原始思考", signature="original-signature"))
    projected = _project(original)
    assert all(block.kind == "text" for message in projected for block in message.blocks)
    assert "来源模型或协议已变化" in projected[0].blocks[0].text
    assert "original-signature" not in repr(projected)
    assert "原始思考" not in repr(projected)
    assert original[0].blocks[0].signature == "original-signature"


def test_current_complete_response_without_thinking_is_not_mistaken_for_legacy() -> None:
    current = _plain_tool_exchange(protocol="claude", model="claude-current")
    assert _project(current) == current
    assert _project(current)[0].blocks[-1].kind == "tool_use"


@pytest.mark.parametrize("protocol,model", [("openai", "other"), ("claude", "claude-older")])
def test_foreign_non_tool_assistant_response_keeps_body_and_drops_thinking(protocol, model) -> None:
    original = ConversationMessage(
        role="assistant", blocks=[ContentBlock.thinking_block("secret", signature="signature"),
                                  ContentBlock.redacted_thinking_block("opaque-data"),
                                  ContentBlock.text_block("原始正文")],
        response_protocol=protocol, response_model=model,
    )
    projected = _project([original])[0]
    assert projected.blocks == [ContentBlock.text_block("原始正文")]
    assert len(original.blocks) == 3


@pytest.mark.parametrize("empty_text", [False, True])
def test_foreign_thinking_only_response_receives_history_notice_instead_of_empty_message(empty_text) -> None:
    original = ConversationMessage(
        role="assistant", blocks=[ContentBlock.thinking_block("secret", signature="signature")],
        response_protocol="claude", response_model="claude-older",
    )
    if empty_text:
        original.blocks.append(ContentBlock.text_block(""))
    projected = _project([original])[0]
    assert len(projected.blocks) == 1 and projected.blocks[0].kind == "text"
    assert "来源模型或协议已变化" in projected.blocks[0].text
    assert "secret" not in projected.blocks[0].text and "signature" not in projected.blocks[0].text


def test_current_non_tool_thinking_response_keeps_exact_signature_and_data() -> None:
    current = ConversationMessage(role="assistant", blocks=_thinking_exchange()[:3],
                                  response_protocol="claude", response_model="claude-current")
    assert _project([current]) == [current]


def test_plain_openai_history_and_plain_assistant_text_need_no_projection() -> None:
    original = [*_plain_tool_exchange(), ConversationMessage.text_message("assistant", "普通正文")]
    assert _project(original, protocol="openai", model="model") == original
    assert _project([original[-1]]) == [original[-1]]


def test_incomplete_exchange_is_never_partially_converted() -> None:
    original = _plain_tool_exchange()
    original[0].blocks.append(ContentBlock.tool_use_block(call_id="still-pending", name="read_file", input={}))
    assert _project(original) == original


def test_mixed_parallel_results_keep_native_pairing_and_text_order() -> None:
    old, old_result = _plain_tool_exchange("old", protocol="openai", model="old-model")
    current, current_result = _plain_tool_exchange("current", protocol="claude", model="claude-current")
    mixed = ConversationMessage(role="tool", blocks=[*old_result.blocks, *current_result.blocks])
    projected = _project([old, current, mixed])
    assert [message.role for message in projected] == ["assistant", "assistant", "user", "tool"]
    assert projected[1] == current
    assert projected[-1].blocks == current_result.blocks
    assert "历史工具返回" in projected[-2].blocks[0].text


def test_reused_call_ids_are_matched_by_occurrence() -> None:
    old = _plain_tool_exchange("reused", protocol="openai", model="old-model")
    current = _plain_tool_exchange("reused", protocol="claude", model="claude-current")
    current[1].blocks[0].text = "新文件原文"
    projected = _project([*old, *current])
    assert projected[-2:] == current
    assert "旧文件原文" in projected[1].blocks[0].text
    assert "新文件原文" not in projected[1].blocks[0].text


@pytest.mark.asyncio
async def test_compaction_after_model_switch_projects_only_summary_request_and_retains_raw_recent_group(
    claude_provider_config, tmp_path,
) -> None:
    class SummaryProvider:
        def __init__(self):
            self.requests = []

        @complete_test_response
        async def stream_chat(self, request):
            self.requests.append(request)
            yield StreamEvent(kind="text_delta", text="<summary>" + "\n".join(
                f"## {heading}\n已总结旧材料。" for heading in SUMMARY_HEADINGS
            ) + "</summary>")
            yield StreamEvent(kind="message_end", response_complete=True)

    old_config = deepcopy(claude_provider_config)
    old_config.model = "claude-older"
    session = SessionController(old_config, cwd=tmp_path)
    provider = SummaryProvider()
    try:
        session.create_user_message("旧材料" + "x" * 50_000)
        old_message = session.create_assistant_message()
        session.append_message_content(old_message.id, "已阅读旧材料。")
        session.complete_message(old_message.id)
        session.create_user_message("当前任务继续检查文件")
        message = session.create_assistant_message()
        session.append_assistant_response(_thinking_exchange())
        session.append_tool_results([ToolExecutionResult(
            call_id="read-1", tool_name="read_file", content="文件原文", is_error=False,
        )])
        session.append_assistant_response([
            ContentBlock.thinking_block("旧模型的最后思考", signature="old-final-signature"),
            ContentBlock.text_block("旧模型已检查文件。"),
        ])
        session.complete_message(message.id, record_transcript=False)
        recent = deepcopy(session.transcript[-4:])
        session.set_model(claude_provider_config, "claude/current")
        result = await session.compact_context(provider=provider, visible_tools=[])
        assert result.after_tokens < result.before_tokens
        assert len(provider.requests) == 1
        sent = provider.requests[0]
        assert sent.model == claude_provider_config.model
        assert all(block.kind not in {"thinking", "redacted_thinking"}
                   and block.signature is None and block.data is None
                   for item in sent.messages for block in item.blocks)
        assert any("来源模型或协议已变化" in block.text for item in sent.messages for block in item.blocks)
        assert session.transcript[-4:] == recent
        assert session.transcript[-1].blocks[0].signature == "old-final-signature"
    finally:
        session.close()
