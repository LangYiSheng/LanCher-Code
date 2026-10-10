"""生成并验证压缩候选；提交与回滚仍由当前会话控制器负责。"""
from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from lancher_code.context.budget import context_budget
from lancher_code.context.compaction import compact_transcript
from lancher_code.context.models import ContextCompactionResult, ContextManagementState
from lancher_code.context.offload import project_tool_results
from lancher_code.context.projection import project_historical_tool_exchanges
from lancher_code.context.prompt_models import PromptContext
from lancher_code.context.request import build_request
from lancher_code.context.tokens import TokenEstimate, estimate_request
from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.messages import ChatRequest, ConversationMessage, StreamEvent
from lancher_code.contracts.tools import DeferredToolGroup, ToolDefinition
from lancher_code.errors import ContextCompactionError
from lancher_code.providers.models import ProviderConfig


@dataclass(slots=True)
class CompactionCandidate:
    transcript: list[ConversationMessage]
    context: ContextManagementState
    result: ContextCompactionResult


async def prepare_compaction(
    *, config: ProviderConfig, prompt_context: PromptContext,
    transcript: list[ConversationMessage], context: ContextManagementState,
    dynamic_context: str | None, before_estimate: TokenEstimate,
    visible_tools: list[ToolDefinition], deferred_tool_groups: list[DeferredToolGroup] | None,
    cancellation_token: CancellationToken | None,
    stream_request: Callable[[ChatRequest], AsyncIterator[StreamEvent]],
    request_factory: Callable[[ChatRequest], ChatRequest],
    prompt_context_factory: Callable[[ContextManagementState], PromptContext] | None = None,
) -> CompactionCandidate:
    class SummaryProvider:
        def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
            outgoing = copy.copy(request)
            outgoing.messages = project_historical_tool_exchanges(
                request.messages, protocol=config.protocol, model=request.model,
            )
            return stream_request(outgoing)

    candidate_context = copy.deepcopy(context)
    # 正文只由核心投影，不写进历史工具输出。成功候选回收正文，保留引用；
    # 提交失败时控制器回滚整个上下文，不能提前让正在使用的技能失效。
    for activation in candidate_context.skill_activations.values():
        activation['body'] = ''
        activation['loaded'] = False
    compacted = await compact_transcript(
        provider=SummaryProvider(), model=config.model,
        transcript=project_tool_results(transcript, context, context_window=config.context_window),
        visible_tools=visible_tools, state=candidate_context, context_window=config.context_window,
        cancellation_token=cancellation_token, request_factory=request_factory,
    )
    # 供应商可能跨轮复用 call_id；近期历史是完整后缀，按出现次数倒序还原原文。
    originals: dict[str, list[str]] = {}
    for message in transcript:
        for block in message.blocks:
            if block.kind == "tool_result" and block.call_id:
                originals.setdefault(block.call_id, []).append(block.text)
    for message in reversed(compacted.transcript):
        for block in reversed(message.blocks):
            values = originals.get(block.call_id)
            if block.kind == "tool_result" and values:
                block.text = values.pop()
    candidate_context.usage_anchor = None
    request = build_request(
        config=config, context=prompt_context_factory(candidate_context) if prompt_context_factory else prompt_context,
        transcript=compacted.transcript, state=candidate_context,
        dynamic_context=dynamic_context, tools=visible_tools, allow_tool_calls=True,
        deferred_tool_groups=deferred_tool_groups,
    )
    after_estimate = estimate_request(request, candidate_context)
    budget = context_budget(config.context_window, request.max_output_tokens)
    if after_estimate.tokens >= before_estimate.tokens or after_estimate.tokens > budget.input_limit:
        raise ContextCompactionError("摘要没有缩小完整请求或仍超出输入预算，已保留原上下文。")
    return CompactionCandidate(compacted.transcript, candidate_context, ContextCompactionResult(
        before_tokens=before_estimate.tokens, after_tokens=after_estimate.tokens,
        dropped_groups=compacted.dropped_groups, before_source=before_estimate.source,
        after_source=after_estimate.source,
    ))
