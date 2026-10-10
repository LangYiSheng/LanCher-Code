from __future__ import annotations

import json
from dataclasses import dataclass

from lancher_code.errors import ToolCallParseError
from lancher_code.contracts.tools import ToolCall, ToolCallChunk, ToolExecutionResult


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

    def finalize_batch(self, *, stop_reason: str | None = None) -> tuple[list[ToolCall], list[ToolExecutionResult]]:
        """先检查整批；坏参数保留原调用身份，任何错误都不执行这一批。"""
        calls: list[ToolCall] = []
        errors: dict[str, str] = {}
        for index in sorted(self._calls):
            partial = self._calls[index]
            if not partial.name.strip() or not partial.call_id:
                raise ToolCallParseError(f"工具调用 #{index} 缺少真实调用编号或工具名，本轮已停止，请重新发送。")
            raw = partial.arguments_json.strip() or "{}"
            reason = None
            try:
                arguments = json.loads(raw)
                if not isinstance(arguments, dict):
                    reason = "工具参数必须是 JSON 对象。"
            except json.JSONDecodeError as exc:
                arguments = None
                reason = f"工具参数不是完整的合法 JSON：{exc.msg}（第 {exc.lineno} 行，第 {exc.colno} 列）。"
            if reason:
                errors[partial.call_id] = reason
            calls.append(ToolCall(
                call_index=index, call_id=partial.call_id, tool_name=partial.name,
                arguments=arguments if isinstance(arguments, dict) else {"INVALID_JSON": raw},
                arguments_json=raw,
            ))
        if len({call.call_id for call in calls}) != len(calls):
            raise ToolCallParseError("工具调用编号重复，本轮已停止，请重新发送。")
        truncated = stop_reason in {"length", "max_tokens", "max_output_tokens"}
        if not errors and not truncated:
            return calls, []
        results = []
        for call in calls:
            malformed = call.call_id in errors
            reason = errors.get(call.call_id, "模型响应达到输出上限。" if truncated else "同批其他工具参数解析失败。")
            feedback = (reason + " 本批工具均未执行。请重新发送完整参数；大文件先用 write_file 写小型骨架，"
                        "读取后用 edit_file 分段填充。write_file 覆盖整文件，不支持追加；不要重发同样的大段内容。")
            results.append(ToolExecutionResult(
                call_id=call.call_id, tool_name=call.tool_name, content=feedback, is_error=True,
                summary="工具参数解析失败" if malformed else "本批工具未执行",
                error_code="tool_call_parse_error" if malformed else "tool_batch_not_executed",
                error_message=feedback, metadata={"started": False, "stop_reason": stop_reason},
            ))
        return calls, results
