from __future__ import annotations

from datetime import datetime, timezone

from lancher_code.context.models import ContextFileSnapshot, ContextManagementState
from lancher_code.context.tokens import truncate_text_tokens
from lancher_code.contracts.tools import ToolDefinition


RECENT_FILE_LIMIT = 5
RECENT_FILE_TOKENS = 5000


def build_recovery_prompt(
    snapshots: list[ContextFileSnapshot],
    visible_tools: list[ToolDefinition],
    *,
    token_budget: int,
) -> str:
    lines = ["继续工作前请使用以下恢复上下文。"]
    lines.append("\n## 最近读取的文件")
    if snapshots:
        for snapshot in snapshots[:RECENT_FILE_LIMIT]:
            lines.extend((f"### {snapshot.path}", f"读取时间：{snapshot.read_at}", snapshot.content))
    else:
        lines.append("暂无可靠文件快照。")
    lines.append("\n## 当前可见工具")
    if visible_tools:
        lines.extend(f"- {tool.name}: {tool.description}" for tool in visible_tools)
    else:
        lines.append("当前请求没有可调用工具。")
    lines.extend(
        (
            "\n## 边界提醒",
            "摘要和文件快照都可能被截断。需要文件、工具结果、错误或用户原话的精确内容时，必须重新调用工具读取，不得猜测。",
        )
    )
    content = "\n".join(lines)
    return truncate_text_tokens(content, token_budget)


def record_file_snapshot(
    state: ContextManagementState,
    *,
    path: str,
    normalized_path: str,
    content: str,
) -> None:
    truncated = truncate_text_tokens(content, RECENT_FILE_TOKENS)
    snapshot = ContextFileSnapshot(
        path=path,
        normalized_path=normalized_path,
        content=truncated,
        read_at=datetime.now(timezone.utc).isoformat(),
    )
    state.recent_files = [
        item for item in state.recent_files if item.normalized_path != normalized_path
    ]
    state.recent_files.insert(0, snapshot)
    del state.recent_files[RECENT_FILE_LIMIT:]
