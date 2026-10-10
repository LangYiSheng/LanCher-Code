"""恢复当前格式中断状态；只修复内存，不重放工具。"""
from __future__ import annotations

from lancher_code.contracts.messages import ContentBlock, ConversationMessage
from lancher_code.sessions.models import SessionState, TraceEntry
from lancher_code.usage.ledger import RequestUsageRecord, summarize_records


def recover_interrupted_history(
    state: SessionState, transcript: list[ConversationMessage]
) -> list[ConversationMessage]:
    """恢复自动保存的活动任务；补齐未知结果，绝不重放工具操作。"""
    for activity in state.compaction_activities.values():
        if activity.status == 'running':
            activity.status = 'interrupted'
            # 恢复时刻不是实际结束时刻，不能用它虚构压缩耗时。
            activity.finished_at = None
            activity.error_text = '上次压缩未完整收尾，请以恢复后的实际上下文为准。'
        if activity.message_id is not None:
            message = next(item for item in state.messages if item.id == activity.message_id)
            if not any(entry.kind == 'compaction' and entry.metadata.get('activity_id') == activity.id
                       for entry in message.trace.entries):
                # 活动事件先于消息轨迹落盘时，中断恢复补上展示位置。
                message.trace.entries.append(TraceEntry(kind='compaction', metadata={'activity_id': activity.id}))
    for record in state.request_usage.values():
        if record['status'] == 'running':
            record['status'] = 'incomplete'
            record['usage']['is_final'] = False
    # 消息用量是账本的派生视图。崩溃可能发生在 usage 事件已落盘、
    # 聊天气泡尚未保存之间，恢复时不能沿用那个过期的显示快照。
    for message in state.messages:
        related = [RequestUsageRecord.from_dict(record) for record in state.request_usage.values()
                   if record.get('message_id') == message.id]
        if related:
            message.usage = summarize_records(related).usage
    interrupted_ids: set[str] = set()
    for message in state.messages:
        if message.role == "assistant" and message.status == "streaming":
            interrupted_ids.add(message.id)
            message.status = "cancelled"
            message.trace.collapsed = True
            if not message.content.strip():
                message.content = "上次任务已中断。"
            for entry in list(message.trace.entries):
                if entry.kind in {"text", "thinking"} and entry.metadata.get("state") == "streaming":
                    entry.metadata["state"] = "cancelled"
                elif entry.kind == "tool_call" and entry.metadata.get("state") in {"queued", "running", "awaiting_permission", "waiting_resources"}:
                    entry.metadata["state"] = "cancelled"
                    entry.metadata.pop("waiting", None)
                    message.trace.entries.append(TraceEntry(
                        kind="tool_result", call_id=entry.call_id, tool_name=entry.tool_name,
                        text="工具结果未完成", ok=False,
                        metadata={"group_id": entry.metadata.get("group_id"), "state": "cancelled",
                                  "started": entry.metadata.get("started", False),
                                  "error_code": "tool_result_interrupted",
                                  "content": "上次任务已中断，未获得完整结果；请先检查操作的实际状态。"},
                    ))
            message.trace.entries.append(TraceEntry(
                kind="notice", text="会话恢复前的任务已中断；请先检查工具操作的实际状态。",
            ))
    if state.plan_snapshot is not None and state.plan_snapshot.source_message_id in interrupted_ids:
        state.plan_snapshot.ready = False

    recovered: list[ConversationMessage] = []
    cursor = 0
    while cursor < len(transcript):
        message = transcript[cursor]
        recovered.append(message)
        cursor += 1
        calls = [block for block in message.blocks if block.kind == "tool_use"]
        if message.role != "assistant" or not calls:
            continue
        # 调用标识可在后续批次复用，只在紧邻本批调用的结果中查找。
        result_message: ConversationMessage | None = None
        recorded_ids: set[str] = set()
        while cursor < len(transcript) and transcript[cursor].role == "tool":
            result_message = transcript[cursor]
            recovered.append(result_message)
            recorded_ids.update(block.call_id for block in result_message.blocks if block.kind == "tool_result")
            cursor += 1
        missing = [call for call in calls if call.call_id not in recorded_ids]
        if not missing:
            continue
        if result_message is None:
            result_message = ConversationMessage(role="tool", blocks=[])
            recovered.append(result_message)
        result_message.blocks.extend(ContentBlock.tool_result_block(
            call_id=call.call_id, is_error=True,
            text="上次任务在保存后中断，未获得此工具调用的完整结果。操作可能已部分执行，请先检查当前状态，勿直接重复执行。",
        ) for call in missing)
    return recovered
