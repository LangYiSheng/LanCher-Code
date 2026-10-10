from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass

from lancher_code.context.budget import context_budget
from lancher_code.context.models import ContextManagementState
from lancher_code.context.recovery import build_recovery_prompt
from lancher_code.context.summary import SUMMARY_REQUEST_PROMPT, SUMMARY_SYSTEM_PROMPT, parse_summary
from lancher_code.context.tokens import estimate_messages_tokens, estimate_request_tokens
from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.messages import ChatRequest, ConversationMessage
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.errors import ContextCompactionError, ProviderPromptTooLongError
from lancher_code.logging_system import get_logger
from lancher_code.providers.base import ChatProvider
from lancher_code.usage.models import MessageUsage


logger = get_logger("context.compaction")
AUTOMATIC_FAILURE_LIMIT = 3


@dataclass(slots=True, frozen=True)
class TranscriptCompaction:
    transcript: list[ConversationMessage]
    dropped_groups: int


async def compact_transcript(
    *,
    provider: ChatProvider,
    model: str,
    transcript: list[ConversationMessage],
    visible_tools: list[ToolDefinition],
    state: ContextManagementState,
    context_window: int,
    cancellation_token: CancellationToken | None = None,
    max_output_tokens: int | None = None,
    request_factory: Callable[[ChatRequest], ChatRequest] | None = None,
) -> TranscriptCompaction:
    transcript = _without_dynamic_reminders(transcript)
    pending_call_ids: set[str] = set()
    for message in transcript:
        for block in message.blocks:
            if block.kind == "tool_use":
                pending_call_ids.add(block.call_id)
            elif block.kind == "tool_result":
                pending_call_ids.discard(block.call_id)
    if pending_call_ids:
        raise ContextCompactionError("存在未完成的工具调用，不能安全压缩上下文。")
    budget = context_budget(context_window, max_output_tokens, purpose="compaction")
    before_tokens = estimate_messages_tokens(transcript)
    groups = group_complete_turns(transcript)
    if not groups:
        raise ContextCompactionError("当前上下文没有可压缩的会话内容。")

    summary_messages = [message for group in groups for message in group]
    dropped_groups = 0
    single_drop_count = 0
    format_error: str | None = None
    while summary_messages:
        if cancellation_token is not None and cancellation_token.is_cancelled:
            raise asyncio.CancelledError
        instruction = SUMMARY_REQUEST_PROMPT
        if format_error is not None:
            # 重新总结同一份历史；不让模型仅凭无效摘要补造缺失事实。
            instruction += f"\n上次摘要结构校验失败：{format_error}。请重新生成完整摘要，缺乏信息的章节写“无”，不要解释重试原因。"
        request = ChatRequest(
            model=model,
            system=[SUMMARY_SYSTEM_PROMPT],
            messages=[*summary_messages, ConversationMessage.text_message("user", instruction)],
            tools=[],
            allow_tool_calls=False,
            thinking=None,
            cancellation_token=cancellation_token,
            max_output_tokens=budget.output_tokens,
            purpose="compaction",
        )
        if estimate_request_tokens(request, ContextManagementState()) > budget.input_limit:
            if format_error is not None:
                raise ContextCompactionError("摘要格式重试超出输入预算，保留原有历史。")
            groups, removed = _drop_oldest_groups(groups, single_drop_count)
            dropped_groups += removed
            single_drop_count += 1
            summary_messages = [message for group in groups for message in group]
            continue
        try:
            if request_factory is not None:
                request = request_factory(request)
            raw_summary = await _collect_summary(provider, request)
        except ProviderPromptTooLongError as exc:
            if format_error is not None:
                raise ContextCompactionError("供应商拒绝摘要格式重试的输入长度，保留原有历史。") from exc
            groups, removed = _drop_oldest_groups(groups, single_drop_count)
            dropped_groups += removed
            single_drop_count += 1
            summary_messages = [message for group in groups for message in group]
            continue
        try:
            summary = parse_summary(raw_summary)
        except ContextCompactionError as exc:
            if format_error is not None:
                raise ContextCompactionError(f"摘要重新生成后仍不符合结构要求：{exc}") from exc
            # 整次压缩最多重试一次格式；截断、网络和工具调用错误不会走到这里。
            format_error = str(exc)
            logger.warning("event=compaction_summary_format_retry reason=%s", format_error)
            continue
        recent = select_recent_history(transcript, token_budget=budget.recent_history_tokens)
        latest_user = next((message for message in reversed(transcript) if message.role == "user"), None)
        if latest_user is not None and not any(message is latest_user for message in recent):
            # 大工具交换可以整组交给摘要，但当前用户原话仍是后续工作的
            # 直接约束。只补回用户消息，不带孤立的工具调用或结果。
            recent = [latest_user, *recent]
        recovery = build_recovery_prompt(
            state.recent_files, visible_tools,
            token_budget=max(128, budget.recent_history_tokens // 2),
        )
        compacted = [
            ConversationMessage.text_message("user", "以下内容是较早会话的压缩历史。"),
            ConversationMessage.text_message("assistant", summary),
            ConversationMessage.text_message("user", recovery),
            *copy.deepcopy(recent),
        ]
        after_tokens = estimate_messages_tokens(compacted)
        if after_tokens >= before_tokens:
            raise ContextCompactionError("摘要没有缩小上下文，保留原有历史。")
        if after_tokens > context_budget(context_window).input_limit:
            raise ContextCompactionError("摘要与恢复内容仍超过可用输入预算，保留原有历史。")
        return TranscriptCompaction(transcript=compacted, dropped_groups=dropped_groups)
    raise ContextCompactionError("上下文过长，已无可用于摘要的完整消息组。")


def group_complete_turns(transcript: list[ConversationMessage]) -> list[list[ConversationMessage]]:
    groups: list[list[ConversationMessage]] = []
    leading: list[ConversationMessage] = []
    pending_call_ids: set[str] = set()
    for message in transcript:
        if message.role == "user" and not pending_call_ids:
            if not groups:
                groups.append([*leading, message])
                leading = []
            else:
                groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            leading.append(message)
        for block in message.blocks:
            if block.kind == "tool_use" and block.call_id:
                pending_call_ids.add(block.call_id)
            elif block.kind == "tool_result":
                pending_call_ids.discard(block.call_id)
    if not groups and leading:
        groups.append(leading)
    return groups


def select_recent_history(
    transcript: list[ConversationMessage], *, token_budget: int,
) -> list[ConversationMessage]:
    groups = group_complete_turns(transcript)
    selected: list[list[ConversationMessage]] = []
    token_count = 0
    for group in reversed(groups):
        group_tokens = estimate_messages_tokens(group)
        if token_count + group_tokens > token_budget:
            break
        selected.append(group)
        token_count += group_tokens
    selected.reverse()
    return [message for group in selected for message in group]


async def _collect_summary(provider: ChatProvider, request: ChatRequest) -> str:
    if request.cancellation_token is not None and request.cancellation_token.is_cancelled:
        raise asyncio.CancelledError
    parts: list[str] = []
    saw_tool_call = False
    completed = False
    usage: MessageUsage | None = None
    stop_reason: str | None = None
    async with aclosing(provider.stream_chat(request)) as stream:
        async for event in stream:
            if request.cancellation_token is not None and request.cancellation_token.is_cancelled:
                raise asyncio.CancelledError
            if event.kind == "text_delta" and event.text:
                parts.append(event.text)
            elif event.kind == "tool_call_delta":
                saw_tool_call = True
            elif event.kind == "message_end":
                completed = event.response_complete
                usage = event.usage
                stop_reason = event.stop_reason
    if request.cancellation_token is not None and request.cancellation_token.is_cancelled:
        raise asyncio.CancelledError
    if saw_tool_call:
        raise ContextCompactionError("摘要请求意外返回了工具调用。")
    if not completed:
        raise ContextCompactionError("摘要响应未正常结束，保留原有历史。")
    if stop_reason in {"length", "max_tokens", "max_output_tokens"} or (
        usage is not None and usage.output_tokens is not None
        and request.max_output_tokens is not None and usage.output_tokens >= request.max_output_tokens
    ):
        raise ContextCompactionError("摘要达到输出上限，可能不完整，保留原有历史。")
    return "".join(parts)


def _drop_oldest_groups(
    groups: list[list[ConversationMessage]],
    attempt: int,
) -> tuple[list[list[ConversationMessage]], int]:
    if len(groups) <= 1:
        return [], len(groups)
    count = 1 if attempt < 3 else max(1, math.ceil(len(groups) * 0.2))
    count = min(count, len(groups) - 1)
    return groups[count:], count


def _without_dynamic_reminders(
    transcript: list[ConversationMessage],
) -> list[ConversationMessage]:
    cleaned: list[ConversationMessage] = []
    for message in copy.deepcopy(transcript):
        if (
            message.role == "user"
            and len(message.blocks) > 1
            and message.blocks[0].kind == "text"
            and message.blocks[0].text.startswith("<system-reminder>\n")
        ):
            message.blocks = message.blocks[1:]
        cleaned.append(message)
    return cleaned
