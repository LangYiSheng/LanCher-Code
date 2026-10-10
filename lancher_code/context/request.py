"""从会话快照构造发送副本，不修改会话历史。"""
from __future__ import annotations

import copy
from datetime import datetime
from urllib.parse import urlsplit

from lancher_code.context.budget import context_budget
from lancher_code.context.models import ContextManagementState
from lancher_code.context.offload import project_tool_results
from lancher_code.context.projection import project_historical_tool_exchanges
from lancher_code.context.prompt_models import PromptContext
from lancher_code.context.prefix import prepare_prefix
from lancher_code.contracts.messages import ChatRequest, ConversationMessage
from lancher_code.contracts.tools import DeferredToolGroup, ToolDefinition
from lancher_code.providers.models import ProviderConfig


def build_request(*, config: ProviderConfig, context: PromptContext,
                  transcript: list[ConversationMessage], state: ContextManagementState,
                  dynamic_context: str | None, tools: list[ToolDefinition], allow_tool_calls: bool,
                  deferred_tool_groups: list[DeferredToolGroup] | None = None,
                  experimental: bool = False, force_reset: bool = False,
                  now: datetime | None = None) -> ChatRequest:
    request, _, _ = prepare_request(config=config, context=context, transcript=copy.deepcopy(transcript),
                                   state=copy.deepcopy(state), dynamic_context=dynamic_context, tools=tools,
                                   allow_tool_calls=allow_tool_calls, deferred_tool_groups=deferred_tool_groups,
                                   experimental=experimental, force_reset=force_reset, now=now)
    return request


def prepare_request(*, config: ProviderConfig, context: PromptContext,
                    transcript: list[ConversationMessage], state: ContextManagementState,
                    dynamic_context: str | None, tools: list[ToolDefinition], allow_tool_calls: bool,
                    deferred_tool_groups: list[DeferredToolGroup] | None = None,
                    experimental: bool = False, force_reset: bool = False,
                    now: datetime | None = None) -> tuple[ChatRequest, ContextManagementState, list[ConversationMessage]]:
    thinking = config.thinking if config.protocol == "claude" else None
    output_tokens = context_budget(config.context_window).output_tokens
    if thinking is not None and thinking.enabled:
        output_tokens = max(output_tokens, thinking.effective_budget_tokens + min(4096, max(256, config.context_window // 32)))
    system, transcript, emitted_tools, updates = prepare_prefix(
        state=state, context=context, transcript=transcript, tools=tools,
        deferred_tool_groups=deferred_tool_groups, dynamic_context=dynamic_context,
        protocol=config.protocol, model=config.model, context_window=config.context_window,
        experimental=experimental, force_reset=force_reset, now=now)
    projected = project_tool_results(transcript, state, context_window=config.context_window)
    for update in updates:
        update['at_message'] = len(project_historical_tool_exchanges(
            projected[:update['at_message']], protocol=config.protocol, model=config.model))
    messages = project_historical_tool_exchanges(projected, protocol=config.protocol, model=config.model)
    request = ChatRequest(model=config.model, system=system, messages=messages, tools=emitted_tools,
                       allow_tool_calls=allow_tool_calls, thinking=thinking, work_phase=context.work_phase,
                       permission_policy=context.permission_policy, session_id=context.session_id,
                        max_output_tokens=output_tokens, experimental_mcp_tool_append=experimental,
                        tool_updates=updates, prompt_cache_enabled=(experimental or config.protocol == 'claude'
                        and urlsplit(config.base_url).hostname == 'api.anthropic.com'))
    return request, state, transcript
