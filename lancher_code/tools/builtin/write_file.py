from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.common import relative_display_path, resolve_path

WRITE_FILE_DESCRIPTION = (
    "写入完整文本文件。适合创建新文件，或在已经完整阅读过旧文件且确认没有外部变更后整体重写文件。"
    "不要用它做局部修改；局部修改应使用 edit_file。"
    "如果目标文件已存在，调用前必须先用 read_file 读过该文件，而且文件自读取后不能被其他地方改动。"
    "新文件不需要先读；不存在的父目录会自动创建。"
    "返回给模型的 content 会说明写入路径和字节数；metadata 会包含绝对路径、相对路径、是否覆盖已有文件等信息供 UI 使用。"
)


class WriteFileTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            description=WRITE_FILE_DESCRIPTION,
            params_model={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要写入的文件路径。支持相对路径和绝对路径。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整文本内容。",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            category="write",
            is_concurrency_safe=False,
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        raw_path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return build_tool_error(
                summary="写文件失败",
                error_code="invalid_arguments",
                error_message="path 必须是非空字符串。",
                tool_name=self.definition.name,
            )
        if not isinstance(content, str):
            return build_tool_error(
                summary="写文件失败",
                error_code="invalid_arguments",
                error_message="content 必须是字符串。",
                tool_name=self.definition.name,
            )

        path = resolve_path(context.cwd, raw_path)
        existed_before = path.exists()
        if existed_before:
            if not path.is_file():
                return build_tool_error(
                    summary=f"路径不是文件: {path}",
                    error_code="not_a_file",
                    error_message=f"路径不是文件: {path}",
                    tool_name=self.definition.name,
                )
            guard_error = _guard_existing_file_write(path, context)
            if guard_error is not None:
                return guard_error

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return build_tool_error(
                summary=f"写入文件失败: {path}",
                error_code="write_error",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        context.file_state_cache.record_write(path, mtime_ns=path.stat().st_mtime_ns)
        byte_count = len(content.encode("utf-8"))
        return build_tool_success(
            summary=f"已写入文件 {path.name}",
            content=f"已写入文件: {path}\n字节数: {byte_count}",
            metadata={
                "path": str(path),
                "relative_path": relative_display_path(path, context.cwd),
                "bytes_written": byte_count,
                "existed_before": existed_before,
            },
            tool_name=self.definition.name,
        )


def _guard_existing_file_write(path: Path, context: ToolContext) -> ToolExecutionResult | None:
    state = context.file_state_cache.get(path)
    if state is None or not state.was_read or state.content is None:
        return build_tool_error(
            summary="写文件失败",
            error_code="stale_file_state",
            error_message="为防止盲写覆盖，写入已有文件前请先使用 read_file 读取该文件。",
            metadata={"path": str(path)},
            tool_name="write_file",
        )
    if not state.is_complete:
        return build_tool_error(
            summary="写文件失败",
            error_code="incomplete_file_read",
            error_message="覆盖写入已有文件前，需要先完整读取该文件；大文件请分段读完后再写。",
            metadata={"path": str(path)},
            tool_name="write_file",
        )
    current_mtime = path.stat().st_mtime_ns
    if state.mtime_ns != current_mtime:
        return build_tool_error(
            summary="写文件失败",
            error_code="file_changed_since_read",
            error_message="文件在读取后已被修改，请重新使用 read_file 读取最新内容后再写入。",
            metadata={"path": str(path), "cached_mtime_ns": state.mtime_ns, "current_mtime_ns": current_mtime},
            tool_name="write_file",
        )
    return None
