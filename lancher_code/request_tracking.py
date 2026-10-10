from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from uuid import uuid4

from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, merge_usage
from lancher_code.providers.base import ChatProvider
from lancher_code.run_usage import RequestUsageRecord


async def tracked_stream(
    provider: ChatProvider, request: ChatRequest, *, protocol: str, run_id: str
) -> AsyncIterator[StreamEvent]:
    """实际尝试只有一个 ID；原生供应商和自定义供应商共享同一记录出口。"""
    # 同一内容可以重试或并发发送，但每一次真实调用都必须有独立身份。
    # 原对象只保留最近启动的 ID；回调与供应商始终操作本次局部副本。
    request.request_id = uuid4().hex
    request = replace(request, run_id=run_id)
    request._prepared_usage_attempt_id = request.request_id
    callback = request.usage_callback
    record = RequestUsageRecord(
        request_id=request.request_id, run_id=run_id, protocol=protocol, model=request.model,
        session_id=request.session_id, turn_id=request.turn_id, message_id=request.message_id,
        purpose=request.purpose, status="running", usage=MessageUsage(is_final=False),
    )
    identity_fields = ("request_id", "run_id", "protocol", "model", "session_id",
                       "turn_id", "message_id", "purpose")
    identity = {name: getattr(record, name) for name in identity_fields}

    def receive(data: dict[str, object]) -> None:
        nonlocal record
        incoming = RequestUsageRecord.from_dict(data)
        if any(getattr(incoming, name) != value for name, value in identity.items()):
            raise ValueError("请求用量回调的身份或归属不一致。")
        record = incoming
        if callback is not None:
            callback(record.to_dict())

    request.usage_callback = receive
    terminal_seen = False
    status = "incomplete"
    try:
        receive(record.to_dict())
        # 提前退出也关闭供应商的生成器，让其 finally 完成请求收尾。
        async with aclosing(provider.stream_chat(request)) as stream:
            async for event in stream:
                if event.kind == "message_end":
                    terminal_seen = True
                    merged = merge_usage(record.usage, event.usage)
                    if merged != record.usage:
                        receive(replace(record, usage=merged).to_dict())
                yield event
        status = "completed" if terminal_seen else "incomplete"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except GeneratorExit:
        if terminal_seen:
            status = "completed"
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
