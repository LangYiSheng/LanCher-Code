"""提供方输入用量校准与模型可见内容的保守粗估。

没有模型 tokenizer 时，估算不可能等同于服务端编码结果。这里仅对新增
内容作分类粗估；最近可信输入用量与其原始请求快照才是校准的依据。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from lancher_code.context.models import ContextManagementState, ContextUsageAnchor
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage
from lancher_code.usage.models import MessageUsage


@dataclass(slots=True, frozen=True)
class TokenEstimate:
    tokens: int
    source: str
    breakdown: dict[str, int]


def estimate_text_tokens(text: str) -> int:
    # ASCII 与中文等非 ASCII 的密度明显不同。此比例是兜底策略，不是假定
    # 某一模型的编码器；实际已知输入会取代整个旧请求的粗估值。
    ascii_count = sum(ord(character) < 128 for character in text)
    return math.ceil(ascii_count / 3 + (len(text) - ascii_count) / 1.5)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _block_content(block: ContentBlock) -> dict[str, object]:
    if block.kind in {"thinking", "redacted_thinking"}:
        # 思考签名和密文是可回传的原始协议内容；哪怕可读文字相同，
        # 它们变化后也不能再套用先前请求的实际输入用量。
        return {"kind": block.kind, "text": block.text, "signature": block.signature,
                "data": block.data, "thinking_protocol": block.thinking_protocol,
                "thinking_field": block.thinking_field}
    if block.kind == "text":
        return {"kind": block.kind, "text": block.text}
    if block.kind == "tool_use":
        return {"kind": block.kind, "id": block.call_id, "name": block.name, "input": block.input}
    return {"kind": block.kind, "id": block.call_id, "text": block.text, "is_error": block.is_error}


def _message_content(message: ConversationMessage) -> dict[str, object]:
    return {"role": message.role, "blocks": [_block_content(block) for block in message.blocks],
            "response_protocol": message.response_protocol, "response_model": message.response_model}


def _shape_digest(request: ChatRequest) -> str:
    # 这些字段影响请求语义、编码方式或模型，但不把内部权限等元数据计入
    # 文本长度。模型变更、工具/系统提示变化都不能沿用先前的用量锚点。
    return _digest({
        "model": request.model,
        "system": request.system,
        "tools": [{"name": tool.name, "description": tool.description, "schema": tool.input_schema} for tool in request.tools],
        "allow_tool_calls": request.allow_tool_calls,
        "thinking": repr(request.thinking),
        "experimental_mcp_tool_append": request.experimental_mcp_tool_append,
        "prompt_cache_enabled": request.prompt_cache_enabled,
        "tool_updates": request.tool_updates,
    })


def _messages_digest(messages: list[ConversationMessage]) -> str:
    return _digest([_message_content(message) for message in messages])


def _message_breakdown(messages: list[ConversationMessage]) -> dict[str, int]:
    result = {"message_text": 0, "thinking": 0, "thinking_metadata": 0,
              "tool_arguments": 0, "tool_results": 0, "framing": 0}
    for message in messages:
        result["framing"] += 8  # 消息角色与提供方聊天模板的保守包装额度。
        for block in message.blocks:
            result["framing"] += 2
            if block.kind == "text":
                result["message_text"] += estimate_text_tokens(block.text)
            elif block.kind in {"thinking", "redacted_thinking"}:
                result["thinking"] += estimate_text_tokens(block.text)
                # 提供方对签名/密文的编码与计费可能不同，这只是未知输入
                # 的保守额度；已上报 usage 仍覆盖完整旧请求的粗估。
                result["thinking_metadata"] += estimate_text_tokens((block.signature or "") + (block.data or ""))
            elif block.kind == "tool_use":
                result["tool_arguments"] += estimate_text_tokens(block.name + block.call_id + _canonical(block.input))
                result["framing"] += 8
            elif block.kind == "tool_result":
                result["tool_results"] += estimate_text_tokens(block.text + block.call_id)
                result["framing"] += 4
    return result


def estimate_messages_tokens(messages: list[ConversationMessage]) -> int:
    return sum(_message_breakdown(messages).values())


def _request_breakdown(request: ChatRequest) -> dict[str, int]:
    result = _message_breakdown(request.messages)
    result["system"] = sum(estimate_text_tokens(text) + 4 for text in request.system)
    result["tool_definitions"] = sum(
        estimate_text_tokens(tool.name) + estimate_text_tokens(tool.description)
        + estimate_text_tokens(_canonical(tool.input_schema)) + 16
        for tool in request.tools
    )
    result['tool_updates'] = sum(estimate_text_tokens(_canonical(event)) + 8 for event in request.tool_updates)
    result["framing"] += 8
    return result


def estimate_request(request: ChatRequest, state: ContextManagementState) -> TokenEstimate:
    anchor = state.usage_anchor
    if anchor is not None:
        if (
            anchor.system_tools_digest == _shape_digest(request)
            and len(request.messages) >= anchor.message_count
            and _messages_digest(request.messages[:anchor.message_count]) == anchor.messages_digest
        ):
            added = _message_breakdown(request.messages[anchor.message_count:])
            calibrated = {"reported_input": anchor.token_count, **added}
            return TokenEstimate(sum(calibrated.values()), "usage_calibrated", calibrated)
        state.usage_anchor = None
    breakdown = _request_breakdown(request)
    return TokenEstimate(sum(breakdown.values()), "estimated", breakdown)


def estimate_request_tokens(request: ChatRequest, state: ContextManagementState) -> int:
    return estimate_request(request, state).tokens


def update_usage_anchor(state: ContextManagementState, request: ChatRequest, usage: MessageUsage) -> bool:
    input_tokens = usage.input_tokens
    if not usage.is_final or not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        return False
    if "input" in usage.partial_fields:
        return False
    if not usage.is_valid:
        return False
    state.usage_anchor = ContextUsageAnchor(
        token_count=input_tokens,
        system_tools_digest=_shape_digest(request),
        message_count=len(request.messages),
        messages_digest=_messages_digest(request.messages),
    )
    return True


def truncate_text_tokens(text: str, token_budget: int) -> str:
    if estimate_text_tokens(text) <= token_budget:
        return text
    marker = "\n[内容已截断，请重新读取原文]"
    available = max(0, token_budget - estimate_text_tokens(marker))
    return text_prefix_for_tokens(text, available) + marker


def text_prefix_for_tokens(text: str, token_budget: int) -> str:
    if estimate_text_tokens(text) <= token_budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]
