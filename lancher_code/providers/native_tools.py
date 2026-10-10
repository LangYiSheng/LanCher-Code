"""把核心保存的工具变更投影到实际支持追加定义的协议。"""
from __future__ import annotations

from copy import deepcopy

from lancher_code.contracts.messages import ChatRequest
from lancher_code.errors import ProviderPromptTooLongError, ProviderResponseError, ProviderRequestError
from lancher_code.providers.base import BaseChatProvider


INLINE_TOOLS_BETA = "inline-tools-2026-09-15"
APPEND_FALLBACK = "请关闭“原生 MCP 工具追加”实验项，使用常规 tools 数组后重新请求；本次不会自动重放操作。"


def tool_definition(raw: dict[str, object]) -> dict[str, object]:
    """只发送模型需要的 schema，不把宿主权限与阶段信息发到远端。"""
    name, description, schema = raw.get("name"), raw.get("description", ""), raw.get("input_schema")
    if not isinstance(name, str) or not name or not isinstance(description, str) or not isinstance(schema, dict):
        raise ProviderRequestError("工具追加事件缺少合法的 name、description 或 input_schema。")
    return {"name": name, "description": description, "input_schema": deepcopy(schema)}


def grouped_tool_updates(request: ChatRequest) -> dict[int, list[dict[str, object]]]:
    """锚点在统一消息中的位置，序列化展开后也不能挪到别处。"""
    if not request.experimental_mcp_tool_append:
        if request.tool_updates:
            raise ProviderRequestError("常规工具模式不能携带原生工具追加事件。")
        return {}
    grouped: dict[int, list[dict[str, object]]] = {}
    previous = -1
    for raw in request.tool_updates:
        if not isinstance(raw, dict):
            raise ProviderRequestError("工具追加事件必须是对象。")
        position = raw.get("at_message")
        additions, removals = raw.get("additions", []), raw.get("removals", [])
        if (type(position) is not int or not 0 <= position <= len(request.messages) or position < previous
                or not isinstance(additions, list) or not isinstance(removals, list)
                or any(not isinstance(item, dict) for item in additions)
                or any(not isinstance(name, str) or not name for name in removals)):
            raise ProviderRequestError("工具追加事件的历史位置或定义不合法。")
        previous = position
        event = {"additions": [tool_definition(item) for item in additions], "removals": list(removals)}
        grouped.setdefault(position, []).append(event)
    return grouped


def claude_update_blocks(events: list[dict[str, object]]) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for event in events:
        blocks.extend({"type": "tool_removal", "tool": {"type": "tool_reference", "name": name}}
                      for name in event["removals"])
        blocks.extend({"type": "tool_addition", "tool": {"type": "tool_definition", "definition": item}}
                      for item in event["additions"])
    return blocks


def responses_tool(raw: dict[str, object]) -> dict[str, object]:
    definition = tool_definition(raw)
    return {"type": "function", "name": definition["name"], "description": definition["description"],
            "parameters": definition["input_schema"], "strict": False}


async def raise_native_error_status(response, *, experimental: bool) -> None:
    try:
        await BaseChatProvider.raise_for_error_status(response)
    except ProviderPromptTooLongError:
        raise
    except ProviderResponseError as exc:
        if experimental and response.status_code in {400, 404, 405, 422}:
            raise ProviderResponseError(f"原生工具追加请求未被端点接受：{exc} {APPEND_FALLBACK}") from exc
        raise
