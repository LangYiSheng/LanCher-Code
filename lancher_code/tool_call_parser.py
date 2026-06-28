from __future__ import annotations

import json
from dataclasses import dataclass

from lancher_code.errors import ToolCallParseError
from lancher_code.models import ToolCall, ToolCallChunk


@dataclass(slots=True)
class _PartialToolCall:
    call_index: int
    call_id: str | None = None
    name: str = ""
    arguments_json: str = ""


class ToolCallAssembler:
    def __init__(self) -> None:
        self._calls: dict[int, _PartialToolCall] = {}

    def consume(self, chunk: ToolCallChunk) -> None:
        partial = self._calls.setdefault(chunk.call_index, _PartialToolCall(call_index=chunk.call_index))
        if chunk.provider_call_id:
            partial.call_id = chunk.provider_call_id
        if chunk.name_delta:
            partial.name += chunk.name_delta
        if chunk.arguments_delta:
            partial.arguments_json += chunk.arguments_delta

    def finalize(self) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for call_index in sorted(self._calls):
            partial = self._calls[call_index]
            if not partial.name.strip():
                raise ToolCallParseError(f"工具调用 #{call_index} 缺少工具名。")

            raw_arguments = partial.arguments_json.strip() or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ToolCallParseError(
                    f"工具 {partial.name} 的参数不是合法 JSON: {raw_arguments}"
                ) from exc

            if not isinstance(arguments, dict):
                raise ToolCallParseError(f"工具 {partial.name} 的参数必须是 JSON 对象。")

            tool_calls.append(
                ToolCall(
                    call_index=call_index,
                    call_id=partial.call_id or f"tool-call-{call_index}",
                    tool_name=partial.name,
                    arguments=arguments,
                    arguments_json=raw_arguments,
                )
            )
        return tool_calls
