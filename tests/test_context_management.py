from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lancher_code.context_management import (
    compact_transcript,
    estimate_request_tokens,
    offload_tool_results,
    parse_summary,
    select_recent_history,
    update_usage_anchor,
)
from lancher_code.errors import ContextCompactionError
from lancher_code.models import (
    ChatRequest,
    ContentBlock,
    ContextManagementState,
    ConversationMessage,
    MessageUsage,
    StreamEvent,
    ToolDefinition,
)


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

    first = await offload_tool_results(transcript, state, tmp_path)
    replacement = state.replacements["../../escape\\name"]
    path = tmp_path / replacement.relative_path

    assert first.offloaded_count == 1
    assert path.is_file()
    assert path.parent == tmp_path / ".lancher" / "context" / "context-test" / "tool-results"
    assert "原始大小：60000 UTF-8 字节" in replacement.preview
    assert "read_file" in replacement.preview
    assert first.transcript[1].blocks[0].text == replacement.preview

    modified = path.stat().st_mtime_ns
    second = await offload_tool_results(transcript, state, tmp_path)
    assert second.offloaded_count == 0
    assert second.transcript[1].blocks[0].text == replacement.preview
    assert path.stat().st_mtime_ns == modified


@pytest.mark.asyncio
async def test_batch_offload_uses_minimum_count_and_stable_order(tmp_path: Path) -> None:
    state = ContextManagementState(context_id="batch")
    transcript = _tool_exchange(
        ["a" * 50_000, "b" * 50_000, "c" * 50_000, "d" * 50_000, "e" * 50_000]
    )

    result = await offload_tool_results(transcript, state, tmp_path)

    assert result.offloaded_count == 1
    assert list(state.replacements) == ["call-0"]
    assert result.transcript[1].blocks[0].text.startswith("[工具结果已卸载]")
    assert result.transcript[2].blocks[0].text == "b" * 50_000


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

    assert incremental >= 120
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
    with pytest.raises(ContextCompactionError):
        parse_summary(f"前缀<summary>{body}</summary>")
    with pytest.raises(ContextCompactionError):
        parse_summary("<summary></summary>")


def test_recent_history_keeps_complete_tool_exchange() -> None:
    transcript = [ConversationMessage.text_message("user", "task"), *_tool_exchange(["x" * 40_000])]
    recent = select_recent_history(transcript)
    assert [message.role for message in recent] == ["user", "assistant", "tool"]


class _SummaryProvider:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.requests: list[ChatRequest] = []

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield StreamEvent(kind="text_delta", text=self.summary)
        yield StreamEvent(kind="message_end")


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
        transcript=[ConversationMessage.text_message("user", "任务")],
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
