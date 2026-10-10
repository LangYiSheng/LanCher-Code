"""测试供应商生成当前协议的完整响应快照。"""
from copy import deepcopy
from contextlib import aclosing
from functools import wraps

from lancher_code.contracts.messages import ContentBlock
from lancher_code.tools.parser import ToolCallAssembler


def complete_test_response(stream):
    """只有测试明确标记成功的终帧才生成协议块；不修补中断流。"""
    @wraps(stream)
    async def wrapped(*args, **kwargs):
        text = []
        assembler = ToolCallAssembler()
        async with aclosing(stream(*args, **kwargs)) as events:
            async for event in events:
                if event.kind == "text_delta" and event.text:
                    text.append(event.text)
                elif event.kind == "tool_call_delta" and event.tool_call_chunk:
                    assembler.consume(event.tool_call_chunk)
                elif event.kind == "message_end" and event.response_complete and event.assistant_blocks is None:
                    event = deepcopy(event)
                    calls, _ = assembler.finalize_batch(stop_reason=event.stop_reason)
                    event.assistant_blocks = ([ContentBlock.text_block("".join(text))] if text else []) + [
                        ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name, input=call.arguments)
                        for call in calls
                    ]
                yield event
    return wrapped
