from __future__ import annotations

from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from lancher_code.agent.events import TurnEvent
from lancher_code.contracts.messages import ChatRequest, ContentBlock
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.errors import ProviderResponseError
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


async def collect_response(*, session: SessionController, provider: ChatProvider,
                           request: ChatRequest, assistant_message_id: str, turn_id: str | None,
                           emit: Callable[[TurnEvent], Awaitable[None]]) -> CollectedResponse:
    """消费一次模型响应，并要求供应商明确提交完整的协议块快照。"""
    assembler = ToolCallAssembler()
    text_parts: list[str] = []
    assistant_blocks = None
    stop_reason = None
    usage = MessageUsage()
    session.bind_usage_request(request, turn_id=turn_id,
                                     message_id=assistant_message_id)
    async with aclosing(session.stream_request(provider, request)) as stream:
        async for event in stream:
            if event.kind == "thinking_delta" and event.text:
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
                if event.response_complete is not True or event.assistant_blocks is None:
                    raise ProviderResponseError("模型响应流提前结束，未收到完整响应；本次工具没有执行，请重试。")
                assistant_blocks = deepcopy(event.assistant_blocks)
                stop_reason = event.stop_reason
                usage = event.usage
    session.finish_trace_segment(assistant_message_id)
    if assistant_blocks is None:
        raise ProviderResponseError("模型响应流没有完整结束，工具未执行。")
    calls, failures = assembler.finalize_batch(stop_reason=stop_reason)
    return CollectedResponse("".join(text_parts), assistant_blocks, usage, calls, failures)
