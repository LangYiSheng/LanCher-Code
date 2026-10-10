"""TUI 测试的完整协议响应脚本；不依赖生产兼容转换。"""
from __future__ import annotations
from copy import deepcopy
import json
from lancher_code.contracts.messages import ContentBlock

def complete_response_script(items):
    """把显式成功脚本展开为 delta 和携带规范 blocks 的完成事件。"""
    if isinstance(items, Exception):
        return items
    result = deepcopy(items)
    blocks = []
    calls = {}
    for item in result:
        event = item[0] if isinstance(item, tuple) else item
        if event.kind in {"text_delta", "thinking_delta"} and event.text:
            kind = "text" if event.kind == "text_delta" else "thinking"
            if blocks and blocks[-1].kind == kind:
                blocks[-1].text += event.text
            elif kind == "text":
                blocks.append(ContentBlock.text_block(event.text))
            else:
                blocks.append(ContentBlock.thinking_block(event.text, protocol="openai", thinking_field="reasoning_content"))
        elif event.kind == "tool_call_delta" and event.tool_call_chunk is not None:
            chunk = event.tool_call_chunk
            if chunk.call_index not in calls:
                block = ContentBlock.tool_use_block(call_id="", name="", input={})
                blocks.append(block)
                calls[chunk.call_index] = (block, "")
            block, arguments = calls[chunk.call_index]
            block.call_id = chunk.provider_call_id or block.call_id
            block.name += chunk.name_delta
            calls[chunk.call_index] = block, arguments + chunk.arguments_delta
        elif event.kind == "message_end" and event.response_complete:
            for block, arguments in calls.values():
                block.input = json.loads(arguments or "{}")
            if event.assistant_blocks is None:
                event.assistant_blocks = deepcopy(blocks)
    return result
