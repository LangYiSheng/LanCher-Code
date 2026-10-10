from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from provider_helpers import complete_test_response

from lancher_code.context.budget import context_budget
from lancher_code.context.compaction import compact_transcript, select_recent_history
from lancher_code.context.models import ContextManagementState
from lancher_code.context.offload import offload_tool_results, project_tool_results
from lancher_code.context.summary import parse_summary
from lancher_code.context.tokens import (
    estimate_request,
    estimate_request_tokens,
    estimate_text_tokens,
    update_usage_anchor,
)
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage, StreamEvent
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.errors import ContextCompactionError
from lancher_code.usage.models import MessageUsage


def _tool_exchange(contents: list[str], call_ids: list[str] | None = None) -> list[ConversationMessage]:
    ids = call_ids or [f"call-{index}" for index in range(len(contents))]
    return [
        ConversationMessage(
            role="assistant",
            blocks=[
                ContentBlock.tool_use_block(call_id=call_id, name="demo", input={})
                for call_id in ids
            ],
        ),
        *[
            ConversationMessage(
                role="tool",
                blocks=[ContentBlock.tool_result_block(call_id=call_id, text=content, is_error=False)],
            )
            for call_id, content in zip(ids, contents, strict=True)
        ],
    ]


@pytest.mark.asyncio
async def test_large_tool_result_is_offloaded_once_with_safe_stable_preview(tmp_path: Path) -> None:
    state = ContextManagementState(context_id="context-test")
    transcript = _tool_exchange(["你" * 20_000], ["../../escape\\name"])

    first = await offload_tool_results(transcript, state, tmp_path, result_directory=tmp_path / "blobs" / "tool-results", context_window=128000)
    first_projected = project_tool_results(transcript, state, context_window=128000)
    replacement = state.replacements["../../escape\\name"]
    path = tmp_path / replacement

    assert first == 1
    assert path.is_file()
    assert path.parent == tmp_path / "blobs" / "tool-results"
    assert "原始大小：60000 UTF-8 字节" in first_projected[1].blocks[0].text
    assert "read_file" in first_projected[1].blocks[0].text
    assert transcript[1].blocks[0].text == "你" * 20_000

    modified = path.stat().st_mtime_ns
    second = await offload_tool_results(transcript, state, tmp_path, result_directory=tmp_path / "blobs" / "tool-results", context_window=128000)
    second_projected = project_tool_results(transcript, state, context_window=128000)
    assert second == 0
    assert second_projected == first_projected
    assert path.stat().st_mtime_ns == modified


@pytest.mark.asyncio
async def test_frozen_tool_preview_does_not_shrink_when_history_grows(tmp_path):
    state = ContextManagementState()
    transcript = _tool_exchange(['first\n' + 'x' * 60000 + '\nlast'], ['initial'])
    await offload_tool_results(transcript, state, tmp_path, result_directory=tmp_path / 'blobs', context_window=128000)
    first = project_tool_results(transcript, state, context_window=128000)
    transcript.extend(_tool_exchange(['y' * 60000] * 9, [f'later-{i}' for i in range(9)]))
    await offload_tool_results(transcript, state, tmp_path, result_directory=tmp_path / 'blobs', context_window=128000)
    grown = project_tool_results(transcript, state, context_window=128000)
    assert grown[:len(first)] == first
    assert len(state.frozen_tool_previews) == 10


def test_tool_definitions_and_native_updates_count_when_calls_are_disabled():
    state = ContextManagementState()
    request = ChatRequest(model='test', allow_tool_calls=False, tools=[ToolDefinition(
        name='builtin', description='固定工具', input_schema={'type': 'object'})])
    initial = estimate_request(request, state)
    assert initial.breakdown['tool_definitions'] > 0
    assert update_usage_anchor(state, request, MessageUsage(input_tokens=100))
    request.experimental_mcp_tool_append = True
    request.tool_updates = [{'at_message': 0, 'additions': [{
        'name': 'mcp_new', 'description': 'x' * 1000, 'input_schema': {'type': 'object'}}], 'removals': []}]
    changed = estimate_request(request, state)
    assert changed.source == 'estimated'
    assert state.usage_anchor is None
    assert changed.breakdown['tool_updates'] > 300
    assert changed.tokens > initial.tokens
    assert update_usage_anchor(state, request, MessageUsage(input_tokens=100))
    request.tool_updates[0]['additions'][0]['description'] = 'different schema'
    assert estimate_request(request, state).source == 'estimated'


@pytest.mark.asyncio
async def test_batch_offload_applies_token_budget_and_preserves_original_order(tmp_path: Path) -> None:
    state = ContextManagementState(context_id="batch")
    transcript = _tool_exchange(
        ["a" * 50_000, "b" * 50_000, "c" * 50_000, "d" * 50_000, "e" * 50_000]
    )

    result = await offload_tool_results(transcript, state, tmp_path, result_directory=tmp_path / "blobs" / "tool-results", context_window=128000)
    result_projected = project_tool_results(transcript, state, context_window=128000)

    assert result == 5
    assert list(state.replacements) == [f"call-{index}" for index in range(5)]
    assert result_projected[1].blocks[0].text.startswith("[工具结果已卸载]")
    assert "完整内容" in result_projected[2].blocks[0].text
    assert transcript[2].blocks[0].text == "b" * 50_000


def test_request_estimate_uses_anchor_only_for_incremental_messages() -> None:
    state = ContextManagementState()
    request = ChatRequest(
        model="test",
        system=["system"],
        messages=[ConversationMessage.text_message("user", "hello")],
    )
    update_usage_anchor(state, request, MessageUsage(input_tokens=100, output_tokens=20))
    request.messages.append(ConversationMessage.text_message("assistant", "more"))

    incremental = estimate_request_tokens(request, state)
    request.system.append("changed")
    full = estimate_request_tokens(request, state)

    assert 100 < incremental < 120
    assert full < incremental


def test_summary_parser_requires_one_nonempty_ordered_nine_part_summary() -> None:
    body = "\n".join(
        f"## {heading}\n内容"
        for heading in (
            "主要请求和意图",
            "关键技术概念",
            "文件和代码段",
            "错误与修复",
            "问题解决过程",
            "用户消息与明确反馈",
            "待办任务",
            "当前工作",
            "可能的下一步",
        )
    )
    assert parse_summary(f"<summary>{body}</summary>") == body
    assert parse_summary(f"以下是摘要：<summary>{body}</summary>") == body
    with pytest.raises(ContextCompactionError):
        parse_summary("<summary></summary>")


def test_recent_history_keeps_complete_tool_exchange() -> None:
    transcript = [ConversationMessage.text_message("user", "task"), *_tool_exchange(["x" * 40_000])]
    recent = select_recent_history(transcript, token_budget=20_000)
    assert [message.role for message in recent] == ["user", "assistant", "tool"]


class _SummaryProvider:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.requests: list[ChatRequest] = []

    @complete_test_response
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield StreamEvent(kind="text_delta", text=self.summary)
        yield StreamEvent(kind="message_end", response_complete=True)


@pytest.mark.asyncio
async def test_compaction_uses_isolated_request_and_builds_recovery_transcript() -> None:
    body = "\n".join(
        f"## {heading}\n内容"
        for heading in (
            "主要请求和意图",
            "关键技术概念",
            "文件和代码段",
            "错误与修复",
            "问题解决过程",
            "用户消息与明确反馈",
            "待办任务",
            "当前工作",
            "可能的下一步",
        )
    )
    provider = _SummaryProvider(f"<summary>{body}</summary>")
    state = ContextManagementState()
    tools = [ToolDefinition(name="read_file", description="读取文件")]

    result = await compact_transcript(
        provider=provider,
        model="test",
        transcript=[ConversationMessage.text_message("user", "任务" * 20_000),
                    ConversationMessage.text_message("assistant", "旧任务完成"),
                    ConversationMessage.text_message("user", "继续任务")],
        visible_tools=tools,
        state=state,
        context_window=128_000,
    )

    request = provider.requests[0]
    assert request.tools == []
    assert request.allow_tool_calls is False
    assert request.thinking is None
    assert [message.role for message in result.transcript[:3]] == ["user", "assistant", "user"]
    assert "read_file: 读取文件" in result.transcript[2].blocks[0].text


def test_calibration_uses_input_snapshot_without_counting_output_twice() -> None:
    state = ContextManagementState()
    request = ChatRequest(model="test", messages=[ConversationMessage.text_message("user", "x" * 3000)])
    update_usage_anchor(state, request, MessageUsage(input_tokens=1000, output_tokens=200))
    assert estimate_request(request, state).tokens == 1000
    request.messages.append(ConversationMessage.text_message("assistant", "x" * 600))
    estimate = estimate_request(request, state)
    assert estimate.source == "usage_calibrated"
    assert estimate.tokens == 1210
    assert estimate.breakdown["reported_input"] == 1000


@pytest.mark.parametrize("usage", [
    MessageUsage(output_tokens=200),
    MessageUsage(input_tokens=10, output_tokens=20, is_final=False),
    MessageUsage(input_tokens=10, output_tokens=20, partial_fields=frozenset({"input"})),
    MessageUsage(input_tokens=10, cached_input_tokens=11, output_tokens=20),
])
def test_untrusted_usage_does_not_establish_or_replace_valid_anchor(usage: MessageUsage) -> None:
    request = ChatRequest(model="test", messages=[ConversationMessage.text_message("user", "hello")])
    empty_state = ContextManagementState()
    assert not update_usage_anchor(empty_state, request, usage)
    assert empty_state.usage_anchor is None
    update_usage_anchor(empty_state, request, MessageUsage(input_tokens=100))
    anchor = empty_state.usage_anchor
    assert not update_usage_anchor(empty_state, request, usage)
    assert empty_state.usage_anchor == anchor
    assert estimate_request(request, empty_state).tokens == 100


@pytest.mark.parametrize("change", ["model", "system", "tools", "prefix", "shorter"])
def test_anchor_invalidates_when_visible_request_snapshot_changes(change: str) -> None:
    request = ChatRequest(model="test", system=["system"], messages=[ConversationMessage.text_message("user", "hello")])
    state = ContextManagementState()
    update_usage_anchor(state, request, MessageUsage(input_tokens=100))
    if change == "model":
        request.model = "different"
    elif change == "system":
        request.system.append("different")
    elif change == "tools":
        request.tools.append(ToolDefinition(name="read_file", description="read"))
    elif change == "prefix":
        request.messages[0].blocks[0].text = "different"
    else:
        request.messages = []
    assert estimate_request(request, state).source == "estimated"
    assert state.usage_anchor is None


def test_estimator_ignores_internal_metadata_and_json_transport_escaping() -> None:
    message = ConversationMessage.text_message("user", "中文\n\"quoted\"")
    tool = ToolDefinition(name="demo", description="description", input_schema={"type": "object"})
    request = ChatRequest(model="test", messages=[message], tools=[tool])
    original = estimate_request(request, ContextManagementState()).tokens
    message.blocks[0].call_id = "internal-only" * 1000
    message.blocks[0].input = {"internal-only": "x" * 1000}
    tool.category = "command"
    tool.should_defer = True
    request.permission_policy = "bypass"
    assert estimate_request(request, ContextManagementState()).tokens == original
    assert estimate_text_tokens("你" * 300) == 200
    assert estimate_text_tokens("x" * 300) == 100
    request.allow_tool_calls = False
    disabled_tool_estimate = estimate_request(request, ContextManagementState()).tokens
    assert disabled_tool_estimate == original
    request.tools = []
    assert estimate_request(request, ContextManagementState()).tokens < disabled_tool_estimate


@pytest.mark.parametrize("window", [8192, 32768, 128000, 1000000])
def test_dynamic_budget_is_positive_and_accounts_for_actual_output(window: int) -> None:
    budget = context_budget(window)
    assert 0 < budget.automatic_threshold < budget.input_limit < window
    assert budget.input_limit + budget.output_tokens < window
    explicit = context_budget(window, max_output_tokens=1000)
    assert explicit.output_tokens == 1000
    assert explicit.input_limit == window - 1000 - min(1024, window // 32)


def test_explicit_output_budget_never_silently_changes_request_cap() -> None:
    budget = context_budget(8192, max_output_tokens=7000)
    assert budget.output_tokens == 7000
    assert budget.input_limit == 936
    impossible = context_budget(8192, max_output_tokens=10000)
    assert impossible.output_tokens == 10000
    assert impossible.input_limit == 0


def test_bounded_recent_history_never_splits_tool_exchange() -> None:
    transcript = [ConversationMessage.text_message("user", "task"), *_tool_exchange(["x" * 4000])]
    assert select_recent_history(transcript, token_budget=100) == []
    assert [message.role for message in select_recent_history(transcript, token_budget=2000)] == ["user", "assistant", "tool"]


@pytest.mark.asyncio
async def test_dynamic_offload_preserves_original_and_retains_both_output_ends(tmp_path: Path) -> None:
    content = "first line\n" + "x" * 4000 + "\nlast error line"
    transcript = _tool_exchange([content])
    state = ContextManagementState()
    result = await offload_tool_results(transcript, state, tmp_path,
                                       result_directory=tmp_path / "blobs", context_window=8192)
    result_projected = project_tool_results(transcript, state, context_window=8192)
    assert result == 1
    assert transcript[1].blocks[0].text == content
    assert (tmp_path / state.replacements["call-0"]).read_text(encoding="utf-8") == content
    assert "first line" in result_projected[1].blocks[0].text
    assert "last error line" in result_projected[1].blocks[0].text
    assert project_tool_results(transcript, state, context_window=8192) == result_projected


def _valid_summary() -> str:
    from lancher_code.context.summary import SUMMARY_HEADINGS
    return "<summary>" + "\n".join(f"## {heading}\n保留结论" for heading in SUMMARY_HEADINGS) + "</summary>"


@pytest.mark.asyncio
async def test_small_window_can_compact_and_binds_summary_request() -> None:
    provider = _SummaryProvider(_valid_summary())
    requests: list[ChatRequest] = []
    def bind(request: ChatRequest) -> ChatRequest:
        requests.append(request)
        return request
    original = [ConversationMessage.text_message("user", "x" * 11000),
                ConversationMessage.text_message("assistant", "旧任务完成"),
                ConversationMessage.text_message("user", "继续任务")]
    result = await compact_transcript(provider=provider, model="test", transcript=original,
                                     visible_tools=[], state=ContextManagementState(), context_window=8192,
                                     request_factory=bind)
    assert len(requests) == 1
    assert requests[0].purpose == "compaction"
    assert requests[0].max_output_tokens == context_budget(8192, purpose="compaction").output_tokens
    assert result.transcript != original
    assert original[0].blocks[0].text == "x" * 11000
    assert result.transcript[-1].blocks[0].text == "继续任务"


@pytest.mark.asyncio
async def test_summary_that_expands_history_is_rejected() -> None:
    original = [ConversationMessage.text_message("user", "short")]
    with pytest.raises(ContextCompactionError, match="没有缩小"):
        await compact_transcript(provider=_SummaryProvider(_valid_summary()), model="test", transcript=original,
                                 visible_tools=[], state=ContextManagementState(), context_window=8192)
    assert original[0].blocks[0].text == "short"


@pytest.mark.asyncio
@pytest.mark.parametrize("end_reason", ["length", "max_tokens"])
async def test_summary_rejects_output_limit_completion(end_reason: str) -> None:
    class Provider(_SummaryProvider):
        @complete_test_response
        async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(kind="text_delta", text=self.summary)
            yield StreamEvent(kind="message_end", stop_reason=end_reason, response_complete=True)
    with pytest.raises(ContextCompactionError, match="输出上限"):
        await compact_transcript(provider=Provider(_valid_summary()), model="test",
                                 transcript=[ConversationMessage.text_message("user", "x" * 11000)],
                                 visible_tools=[], state=ContextManagementState(), context_window=8192)


@pytest.mark.asyncio
async def test_summary_requires_completed_stream() -> None:
    class Provider(_SummaryProvider):
        @complete_test_response
        async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
            yield StreamEvent(kind="text_delta", text=self.summary)
    with pytest.raises(ContextCompactionError, match="未正常结束"):
        await compact_transcript(provider=Provider(_valid_summary()), model="test",
                                 transcript=[ConversationMessage.text_message("user", "x" * 11000)],
                                 visible_tools=[], state=ContextManagementState(), context_window=8192)


@pytest.mark.asyncio
async def test_offloaded_preview_adapts_after_switching_to_smaller_window(tmp_path: Path) -> None:
    transcript = _tool_exchange(["first\n" + "x" * 60000 + "\nlast"])
    state = ContextManagementState()
    await offload_tool_results(transcript, state, tmp_path,
                              result_directory=tmp_path / "blobs", context_window=128000)
    large = project_tool_results(transcript, state, context_window=128000)
    # 已发送的预览在同一 epoch 内冻结；切换模型/窗口会先重建 epoch。
    assert project_tool_results(transcript, state, context_window=8192) == large
    state.frozen_tool_previews.clear()
    small = project_tool_results(transcript, state, context_window=8192)
    assert len(small[1].blocks[0].text) < len(large[1].blocks[0].text)
    assert "first" in small[1].blocks[0].text and "last" in small[1].blocks[0].text
    assert len(transcript[1].blocks[0].text) > 60000


@pytest.mark.asyncio
async def test_compaction_never_drops_last_oversized_user_group_to_succeed() -> None:
    original = [ConversationMessage.text_message("user", "旧任务"),
                ConversationMessage.text_message("assistant", "完成"),
                ConversationMessage.text_message("user", "current instruction " + "x" * 100000)]
    provider = _SummaryProvider(_valid_summary())
    with pytest.raises(ContextCompactionError, match="已无可用于摘要"):
        await compact_transcript(provider=provider, model="test", transcript=original,
                                 visible_tools=[], state=ContextManagementState(), context_window=8192)
    assert provider.requests == []
    assert original[-1].blocks[0].text.startswith("current instruction ")


@pytest.mark.asyncio
async def test_large_recent_tool_group_preserves_current_user_without_orphan_results() -> None:
    original = [ConversationMessage.text_message("user", "x" * 8000),
                ConversationMessage.text_message("assistant", "旧任务完成"),
                ConversationMessage.text_message("user", "不要改数据库，只分析错误"),
                *_tool_exchange(["x" * 7000])]
    result = await compact_transcript(provider=_SummaryProvider(_valid_summary()), model="test", transcript=original,
                                      visible_tools=[], state=ContextManagementState(), context_window=8192)
    assert result.transcript[-1].blocks[0].text == "不要改数据库，只分析错误"
    assert not any(block.kind in {"tool_use", "tool_result"} for message in result.transcript for block in message.blocks)


@pytest.mark.asyncio
async def test_incomplete_tool_call_prevents_summary_request() -> None:
    original = [ConversationMessage.text_message("user", "task"),
                ConversationMessage(role="assistant", blocks=[ContentBlock.tool_use_block(call_id="pending", name="demo", input={})])]
    provider = _SummaryProvider(_valid_summary())
    with pytest.raises(ContextCompactionError, match="未完成的工具调用"):
        await compact_transcript(provider=provider, model="test", transcript=original,
                                 visible_tools=[], state=ContextManagementState(), context_window=8192)
    assert provider.requests == []


def test_user_message_between_call_and_result_does_not_split_exchange() -> None:
    from lancher_code.context.compaction import group_complete_turns
    exchange = _tool_exchange(["result"])
    transcript = [ConversationMessage.text_message("user", "task"), exchange[0],
                  ConversationMessage.text_message("user", "follow-up"), exchange[1],
                  ConversationMessage.text_message("user", "next task")]
    groups = group_complete_turns(transcript)
    assert [message.role for message in groups[0]] == ["user", "assistant", "user", "tool"]
    assert [message.role for message in groups[1]] == ["user"]


def _tool_text_tokens(transcript: list[ConversationMessage]) -> int:
    return sum(estimate_text_tokens(block.text) for message in transcript
               for block in message.blocks if block.kind == "tool_result")


@pytest.mark.asyncio
async def test_batch_budget_counts_selected_previews_instead_of_treating_them_as_free(tmp_path: Path) -> None:
    transcript = _tool_exchange(["x" * 2500 for _ in range(10)])
    state = ContextManagementState()
    result = await offload_tool_results(transcript, state, tmp_path,
                                       result_directory=tmp_path / "blobs", context_window=8192)
    result_projected = project_tool_results(transcript, state, context_window=8192)
    assert _tool_text_tokens(result_projected) <= context_budget(8192).tool_batch_tokens
    assert result > 8
    assert all(message.blocks[0].text == "x" * 2500 for message in transcript[1:])
    assert len(result_projected[0].blocks) == len(result_projected[1:]) == 10


@pytest.mark.asyncio
async def test_existing_previews_and_new_results_share_smaller_window_budget(tmp_path: Path) -> None:
    old_ids = [f"old-{index}" for index in range(5)]
    transcript = _tool_exchange(["x" * 40000 for _ in old_ids], old_ids)
    state = ContextManagementState()
    first = await offload_tool_results(transcript, state, tmp_path,
                                      result_directory=tmp_path / "blobs", context_window=128000)
    assert first == 5
    transcript.extend(_tool_exchange(["x" * 2500 for _ in range(5)], [f"new-{index}" for index in range(5)]))
    second = await offload_tool_results(transcript, state, tmp_path,
                                       result_directory=tmp_path / "blobs", context_window=8192)
    second_projected = project_tool_results(transcript, state, context_window=8192)
    assert _tool_text_tokens(second_projected) <= context_budget(8192).tool_batch_tokens
    assert second == 5
    assert len(state.replacements) == 10
    assert all((tmp_path / state.replacements[call_id]).read_text(encoding="utf-8") == "x" * 40000 for call_id in old_ids)


@pytest.mark.asyncio
async def test_unreachable_reference_budget_keeps_pairs_for_full_request_hard_check(tmp_path: Path) -> None:
    transcript = _tool_exchange(["x" * 1000 for _ in range(5)])
    state = ContextManagementState()
    result = await offload_tool_results(transcript, state, tmp_path,
                                       result_directory=tmp_path / "blobs", context_window=256)
    result_projected = project_tool_results(transcript, state, context_window=256)
    assert result == 5
    assert len(result_projected[0].blocks) == len(result_projected[1:]) == 5
    assert _tool_text_tokens(result_projected) > context_budget(256).tool_batch_tokens
    assert all("完整内容：" in message.blocks[0].text and "read_file" in message.blocks[0].text
               for message in result_projected[1:])
    request = ChatRequest(model="test", messages=result_projected)
    assert estimate_request_tokens(request, ContextManagementState()) > context_budget(256).input_limit
