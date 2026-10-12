from __future__ import annotations

from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from lancher_code.agent.events import TurnEvent
from lancher_code.contracts.messages import (
    ChatRequest, ContentBlock, STOP_REASON_MESSAGES, output_limit_reached,
)
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.errors import ProviderResponseError, ToolCallParseError
from lancher_code.providers.base import ChatProvider
from lancher_code.sessions.controller import SessionController
from lancher_code.tools.parser import ToolCallAssembler
from lancher_code.usage.models import MessageUsage


@dataclass(slots=True)
class CollectedResponse:
    text: str
    assistant_blocks: list[ContentBlock]
    usage: MessageUsage
    tool_calls: list[ToolCall]
    precomputed_results: list[ToolExecutionResult]
    stop_reason: str | None = None
    output_limited: bool = False


async def collect_response(*, session: SessionController, provider: ChatProvider,
                           request: ChatRequest, assistant_message_id: str, turn_id: str | None,
                           emit: Callable[[TurnEvent], Awaitable[None]]) -> CollectedResponse:
    """消费一次模型响应，并要求供应商明确提交完整的协议块快照。"""
    assembler = ToolCallAssembler()
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    assistant_blocks = None
    stop_reason = None
    protocol_complete = False
    usage = MessageUsage()
    session.bind_usage_request(request, turn_id=turn_id,
                                     message_id=assistant_message_id)
    async with aclosing(session.stream_request(provider, request)) as stream:
        async for event in stream:
            if event.kind == "thinking_delta" and event.text:
                thinking_parts.append(event.text)
                session.append_trace_thinking(assistant_message_id, event.text)
                await emit(TurnEvent(
                        kind="progress_updated",
                        message=session.get_message(assistant_message_id),
                        usage=deepcopy(session.get_message(assistant_message_id).usage),
                        progress_message="模型正在思考",
                    ),
                )
            elif event.kind == "text_delta" and event.text:
                text_parts.append(event.text)
                session.append_message_content(assistant_message_id, event.text)
                await emit(TurnEvent(
                        kind="assistant_text_delta",
                        message=session.get_message(assistant_message_id),
                        usage=deepcopy(session.get_message(assistant_message_id).usage),
                        text=event.text,
                    ),
                )
            elif event.kind == "tool_call_delta" and event.tool_call_chunk:
                message = session.get_message(assistant_message_id)
                entries = message.trace.entries
                if entries and entries[-1].kind in {"thinking", "text"} and entries[-1].metadata.get("state") == "streaming":
                    session.finish_trace_segment(assistant_message_id)
                    await emit(TurnEvent(
                        kind="progress_updated", message=message,
                        usage=deepcopy(session.get_message(assistant_message_id).usage),
                        progress_message="正在准备工具调用",
                    ))
                assembler.consume(event.tool_call_chunk)
            elif event.kind == "message_end":
                session.finish_trace_segment(assistant_message_id)
                stop_reason = event.stop_reason
                usage = event.usage
                protocol_complete = event.response_complete is True and event.assistant_blocks is not None
                if stop_reason in STOP_REASON_MESSAGES:
                    raise ProviderResponseError(STOP_REASON_MESSAGES[stop_reason] + "；已有内容已保留，本轮未完成。")
                if (event.response_complete is True or event.terminal_received) and output_limit_reached(
                    stop_reason, usage, request.max_output_tokens
                ):
                    # 明确的额度耗尽可以续接，EOF/网络断流不能冒充可继续的终帧。
                    # Responses incomplete 没有完整协议快照，只保存带说明的文字，
                    # 不从半截参数或思考签名制造可执行的工具交换。
                    if event.response_complete is not True or event.assistant_blocks is None:
                        partial = "".join(thinking_parts)
                        assistant_blocks = ([ContentBlock.text_block(
                            "以下为输出额度耗尽前的未完成思考，仅供续接参考：\n" + partial
                        )] if partial else [])
                        if text_parts:
                            assistant_blocks.append(ContentBlock.text_block("".join(text_parts)))
                        continue
                if event.response_complete is not True or event.assistant_blocks is None:
                    raise ProviderResponseError("模型响应流提前结束，未收到完整响应；本次工具没有执行，请重试。")
                assistant_blocks = deepcopy(event.assistant_blocks)
            elif event.kind == "error":
                raise ProviderResponseError(event.text or "模型供应商报告生成错误；已有内容已保留，本轮未完成。")
    session.finish_trace_segment(assistant_message_id)
    if assistant_blocks is None:
        raise ProviderResponseError("模型响应流没有完整结束，工具未执行。")
    limited = output_limit_reached(stop_reason, usage, request.max_output_tokens)
    if limited and not protocol_complete:
        # 半截工具调用没有可提交的协议身份；整批留在界面，下一次重新生成。
        calls, failures = [], []
    else:
        try:
            calls, failures = assembler.finalize_batch(stop_reason="max_output_tokens" if limited else stop_reason)
        except ToolCallParseError:
            if not limited:
                raise
            # 标识也被截断时无法配对原生工具反馈，只引用未完成内容再生成。
            assistant_blocks = [block for block in assistant_blocks if block.kind != "tool_use"]
            assistant_blocks.append(ContentBlock.text_block(
                "输出额度耗尽时工具调用标识或名称尚未完整，本批工具均未执行；后续必须重新发送完整调用。"))
            calls, failures = [], []
    if not limited and not calls and not "".join(text_parts).strip():
        raise ProviderResponseError("模型已结束响应，但没有返回正文或工具调用；已有思考已保留，请重试。")
    return CollectedResponse("".join(text_parts), assistant_blocks, usage, calls, failures, stop_reason, limited)
