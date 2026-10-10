from __future__ import annotations

from lancher_code.tools.context import ToolContext
from lancher_code.contracts.tools import ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.filesystem.access import PathWriteDeniedError, ensure_writable_path, is_session_workspace_path, relative_display_path
from lancher_code.tools.core.common import atomic_write_text

WRITE_PLAN_FILE_DESCRIPTION = (
    "覆盖写入计划文件。"
    "这个工具只在 plan 模式下可用，只能写入预设的计划文件路径。"
    "参数只有 content，必须提供完整计划文本。"
)


class WritePlanFileTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_plan_file",
            description=WRITE_PLAN_FILE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "要覆盖写入计划文件的完整文本。",
                    }
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            category="write",
            allowed_phases=("plan",),
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        if context.plan_file_path is None or not is_session_workspace_path(context.plan_file_path, context):
            return build_tool_error(
                summary="写入计划文件失败",
                error_code="missing_plan_file_path",
                error_message="当前上下文没有有效的会话计划文件路径。",
                tool_name=self.definition.name,
            )

        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return build_tool_error(
                summary="写入计划文件失败",
                error_code="invalid_arguments",
                error_message="content 必须是包含计划正文的非空字符串。",
                tool_name=self.definition.name,
            )

        try:
            path = ensure_writable_path(context.plan_file_path, context)
        except ValueError as exc:
            return build_tool_error(
                summary="写入计划文件失败",
                error_code=exc.reason_code if isinstance(exc, PathWriteDeniedError) else "path_outside_project",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        try:
            existed = path.exists()
            atomic_write_text(path, content, context, expected_exists=existed,
                              expected_mtime_ns=path.stat().st_mtime_ns if existed else None)
        except PathWriteDeniedError as exc:
            return build_tool_error(summary="写入计划文件失败", error_code=exc.reason_code,
                                    error_message=str(exc), tool_name=self.definition.name)
        except OSError as exc:
            return build_tool_error(
                summary="写入计划文件失败",
                error_code="write_error",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        byte_count = len(content.encode("utf-8"))
        return build_tool_success(
            summary=f"已写入计划文件 {path.name}",
            content=f"已写入计划文件 {path}\n字节数: {byte_count}",
            metadata={
                "path": str(path),
                "relative_path": relative_display_path(path, context.cwd),
                "bytes_written": byte_count,
            },
            tool_name=self.definition.name,
        )
