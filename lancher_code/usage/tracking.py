from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from uuid import uuid4

from lancher_code.contracts.messages import ChatRequest, StreamEvent, generation_incomplete
from lancher_code.usage.models import MessageUsage, merge_usage
from lancher_code.providers.base import ChatProvider
from lancher_code.usage.ledger import RequestUsageRecord


async def tracked_stream(
    provider: ChatProvider, request: ChatRequest, *, protocol: str, run_id: str
) -> AsyncIterator[StreamEvent]:
    """实际尝试只有一个 ID；原生供应商和自定义供应商共享同一记录出口。"""
    # 同一内容可以重试或并发发送，但每一次真实调用都必须有独立身份。
    # 原对象只保留最近启动的 ID；回调与供应商始终操作本次局部副本。
    request.request_id = uuid4().hex
    request = replace(request, run_id=run_id)
    request.prepare_usage_attempt()
    callback = request.usage_callback
    record = RequestUsageRecord(
        request_id=request.request_id, run_id=run_id, protocol=protocol, model=request.model,
        session_id=request.session_id, turn_id=request.turn_id, message_id=request.message_id,
        purpose=request.purpose, status="running", usage=MessageUsage(is_final=False),
        max_output_tokens=request.max_output_tokens,
    )
    identity_fields = ("request_id", "run_id", "protocol", "model", "session_id",
                       "turn_id", "message_id", "purpose")
    identity = {name: getattr(record, name) for name in identity_fields}

    def receive(data: dict[str, object]) -> None:
        nonlocal record
        incoming = RequestUsageRecord.from_dict(data)
        if any(getattr(incoming, name) != value for name, value in identity.items()):
            raise ValueError("请求用量回调的身份或归属不一致。")
        # 原生供应商可补齐实际默认额度；后续回调不能丢失已收到的结束原因。
        if incoming.max_output_tokens is None:
            incoming.max_output_tokens = record.max_output_tokens
        if incoming.stop_reason is None:
            incoming.stop_reason = record.stop_reason
        if incoming.status == "completed" and generation_incomplete(
            incoming.stop_reason, incoming.usage, incoming.max_output_tokens
        ):
            incoming.status = "incomplete"
        record = incoming
        if callback is not None:
            callback(record.to_dict())

    request.usage_callback = receive
    terminal_seen = False
    generation_unfinished = False
    status = "incomplete"
    try:
        receive(record.to_dict())
        # 提前退出也关闭供应商的生成器，让其 finally 完成请求收尾。
        async with aclosing(provider.stream_chat(request)) as stream:
            async for event in stream:
                if event.kind == "message_end":
                    terminal_seen = event.response_complete is True
                    merged = merge_usage(record.usage, event.usage)
                    reason = event.stop_reason or record.stop_reason
                    generation_unfinished = generation_incomplete(reason, merged, record.max_output_tokens)
                    receive(replace(record, usage=merged, stop_reason=reason).to_dict())
                yield event
        status = "completed" if terminal_seen and not generation_unfinished else "incomplete"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except GeneratorExit:
        if terminal_seen:
            status = "incomplete" if generation_unfinished else "completed"
        elif request.cancellation_token is not None and request.cancellation_token.is_cancelled:
            status = "cancelled"
        else:
            status = "incomplete"
        raise
    except BaseException:
        status = "failed"
        raise
    finally:
        try:
            if record.status == "running":
                receive(replace(record, status=status).to_dict())
        finally:
            request.usage_callback = callback
