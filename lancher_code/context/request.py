"""从会话快照构造发送副本，不修改会话历史。"""
from __future__ import annotations

from lancher_code.context.budget import context_budget
from lancher_code.context.models import ContextManagementState
from lancher_code.context.offload import project_tool_results
from lancher_code.context.projection import project_historical_tool_exchanges
from lancher_code.context.prompt_models import PromptContext
from lancher_code.context.prompts import build_chat_request_payload
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage
from lancher_code.contracts.tools import DeferredToolGroup, ToolDefinition, tool_available_in_phase
from lancher_code.providers.models import ProviderConfig


def build_request(*, config: ProviderConfig, context: PromptContext,
                  transcript: list[ConversationMessage], state: ContextManagementState,
                  dynamic_context: str | None, tools: list[ToolDefinition], allow_tool_calls: bool,
                  deferred_tool_groups: list[DeferredToolGroup] | None = None) -> ChatRequest:
    thinking = config.thinking if config.protocol == "claude" else None
    output_tokens = context_budget(config.context_window).output_tokens
    if thinking is not None and thinking.enabled:
        output_tokens = max(output_tokens, thinking.effective_budget_tokens + min(4096, max(256, config.context_window // 32)))
    messages = project_tool_results(transcript, state, context_window=config.context_window)
    for message in messages:
        if (message.role == "user" and len(message.blocks) > 1 and message.blocks[0].kind == "text"
                and message.blocks[0].text.startswith("<system-reminder>\n")):
            message.blocks = message.blocks[1:]
    if dynamic_context:
        for message in reversed(messages):
            if message.role == "user":
                message.blocks.insert(0, ContentBlock.text_block(dynamic_context))
                break
    messages = project_historical_tool_exchanges(messages, protocol=config.protocol, model=config.model)
    payload = build_chat_request_payload(
        context=context, transcript=messages,
        tools=[tool for tool in tools if tool_available_in_phase(tool, context.work_phase)] if allow_tool_calls else [],
        deferred_tool_groups=deferred_tool_groups,
    )
    return ChatRequest(model=config.model, system=payload.system, messages=payload.messages, tools=payload.tools,
                       allow_tool_calls=allow_tool_calls, thinking=thinking, work_phase=context.work_phase,
                       permission_policy=context.permission_policy, session_id=context.session_id,
                       max_output_tokens=output_tokens)
