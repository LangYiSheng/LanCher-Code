"""消息正文与有序轨迹的内存变更，不处理保存、模型请求和会话切换。"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.sessions.models import SessionMessage, TraceEntry
from lancher_code.usage.models import MessageUsage, add_usage


class MessageEditor:
    def __init__(self, messages: list[SessionMessage]) -> None:
        self._messages = messages

    def create_assistant_message(self) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="assistant",
            content="",
            status="streaming",
            timestamp=self._now(),
        )
        self._messages.append(message)
        return message

    def append_message_content(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content += delta
        self.append_trace_text(message_id, delta)
        return message

    def clear_message_content(self, message_id: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content = ""
        return message

    def add_message_usage(self, message_id: str, usage: MessageUsage) -> SessionMessage:
        message = self.get_message(message_id)
        message.usage = add_usage(message.usage, usage) if message.usage.known_fields else copy.deepcopy(usage)
        return message

    def append_trace_thinking(self, message_id: str, delta: str) -> SessionMessage:
        return self._append_stream_segment(message_id, "thinking", delta)

    def append_trace_text(self, message_id: str, text: str) -> SessionMessage:
        return self._append_stream_segment(message_id, "text", text)

    def _append_stream_segment(self, message_id: str, kind: Literal["text", "thinking"], text: str) -> SessionMessage:
        message = self.get_message(message_id)
        if text:
            self.expand_trace_on_first_entry(message)
            entries = message.trace.entries
            if entries and entries[-1].kind == kind and entries[-1].metadata.get("state") == "streaming":
                entries[-1].text += text
            else:
                self.finish_trace_segment(message_id)
                entries.append(TraceEntry(kind=kind, text=text, metadata={"state": "streaming"}))
        return message

    def finish_trace_segment(self, message_id: str, state: str = "complete") -> SessionMessage:
        """封口当前输出段，下一条同类输出也不会跨响应合并。"""
        message = self.get_message(message_id)
        if message.trace.entries:
            entry = message.trace.entries[-1]
            if entry.kind in {"thinking", "text"} and entry.metadata.get("state") == "streaming":
                entry.metadata["state"] = state
        return message

    def append_trace_notice(self, message_id: str, text: str) -> SessionMessage:
        message = self.finish_trace_segment(message_id)
        self.expand_trace_on_first_entry(message)
        message.trace.entries.append(TraceEntry(kind="notice", text=text))
        return message

    def append_trace_tool_calls(self, message_id: str, tool_calls: list[ToolCall]) -> SessionMessage:
        message = self.finish_trace_segment(message_id)
        self.expand_trace_on_first_entry(message)
        group_id = uuid4().hex
        for call in tool_calls:
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_call",
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    metadata={"group_id": group_id, "state": "queued", "started": False},
                )
            )
        return message

    def set_trace_tool_state(self, message_id: str, call_id: str, state: str,
                             *, waiting: dict | None = None, invocation_id: str | None = None) -> SessionMessage:
        message = self.get_message(message_id)
        for entry in reversed(message.trace.entries):
            if entry.kind == "tool_call" and entry.call_id == call_id:
                entry.metadata["state"] = state
                if invocation_id is not None:
                    entry.metadata["invocation_id"] = invocation_id
                if waiting and state in {"queued", "waiting_resources"}:
                    # 等待快照属于这次状态，后续调度变化不能改写已保存的说明。
                    entry.metadata["waiting"] = copy.deepcopy(waiting)
                else:
                    entry.metadata.pop("waiting", None)
                if state == "running":
                    entry.metadata["started"] = True
                break
        return message

    def append_trace_tool_results(self, message_id: str, results: list[ToolExecutionResult]) -> SessionMessage:
        message = self.get_message(message_id)
        self.expand_trace_on_first_entry(message)
        for result in results:
            call_entry = next((entry for entry in reversed(message.trace.entries)
                               if entry.kind == "tool_call" and entry.call_id == result.call_id), None)
            state = "complete" if (not result.is_error) else "error"
            if result.error_code == "steering_superseded":
                state = "skipped"
            elif result.error_code == "tool_result_interrupted":
                state = "cancelled"
            elif result.error_code == 'mcp_outcome_unknown':
                state = 'unknown'
            metadata = {**result.metadata, "content": result.content, "state": state,
                        "error_code": result.error_code, "error_message": result.error_message}
            metadata.pop("waiting", None)
            if call_entry is not None:
                call_entry.metadata["state"] = state
                call_entry.metadata.pop("waiting", None)
                metadata["group_id"] = call_entry.metadata.get("group_id")
                metadata["started"] = call_entry.metadata.get("started", False)
                if call_entry.metadata.get("invocation_id"):
                    metadata["invocation_id"] = call_entry.metadata["invocation_id"]
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_result",
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    text=result.summary if (not result.is_error) else (result.error_message or result.summary),
                    metadata=metadata,
                    ok=(not result.is_error),
                )
            )
        return message

    @staticmethod
    def has_last_notice(message: SessionMessage, text: str) -> bool:
        return bool(message.trace.entries and message.trace.entries[-1].kind == "notice"
                    and message.trace.entries[-1].text == text)

    def get_message(self, message_id: str) -> SessionMessage:
        for message in self._messages:
            if message.id == message_id:
                return message
        raise KeyError(f"未找到消息：{message_id}")

    @staticmethod
    def expand_trace_on_first_entry(message: SessionMessage) -> None:
        if message.role == "assistant" and message.status == "streaming" and not message.trace.entries:
            message.trace.collapsed = False

    @staticmethod
    def _new_message_id() -> str:
        return uuid4().hex

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
