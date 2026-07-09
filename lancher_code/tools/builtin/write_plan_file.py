from __future__ import annotations

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.common import ensure_path_in_root, relative_display_path

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
            params_model={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要覆盖写入计划文件的完整文本。",
                    }
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            category="write",
            is_concurrency_safe=False,
            allowed_modes=("plan",),
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        if context.plan_file_path is None:
            return build_tool_error(
                summary="写入计划文件失败",
                error_code="missing_plan_file_path",
                error_message="当前上下文没有配置计划文件路径。",
                tool_name=self.definition.name,
            )

        content = arguments.get("content")
        if not isinstance(content, str):
            return build_tool_error(
                summary="写入计划文件失败",
                error_code="invalid_arguments",
                error_message="content 必须是字符串。",
                tool_name=self.definition.name,
            )

        try:
            path = ensure_path_in_root(context.plan_file_path, context.project_root or context.cwd)
        except ValueError as exc:
            return build_tool_error(
                summary="写入计划文件失败",
                error_code="path_outside_project",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
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
