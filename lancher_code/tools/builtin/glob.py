from __future__ import annotations

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.common import (
    MODEL_PATH_LIMIT,
    MODEL_TEXT_CHAR_LIMIT,
    UI_PATH_LIMIT,
    is_skipped_path,
    relative_display_path,
    resolve_path_in_root,
)

GLOB_DESCRIPTION = (
    "按 glob 模式查找文件，支持 ** 递归匹配。"
    "适合在不知道准确文件名时先缩小范围。"
    "不要用它搜索文件内容；搜索内容应该使用 grep。"
)


class GlobTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="glob",
            description=GLOB_DESCRIPTION,
            params_model={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 模式，例如 **/*.py 或 src/**/*.ts。",
                    },
                    "path": {
                        "type": "string",
                        "description": "可选。限制搜索根目录，支持相对路径和绝对路径。",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            category="read",
            is_concurrency_safe=True,
            allowed_modes=("default", "plan", "acceptEdits", "bypass"),
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        pattern = arguments.get("pattern")
        raw_path = arguments.get("path")
        if not isinstance(pattern, str) or not pattern.strip():
            return build_tool_error(
                summary="查找文件失败",
                error_code="invalid_arguments",
                error_message="pattern 必须是非空字符串。",
                tool_name=self.definition.name,
            )

        search_root = context.project_root or context.cwd
        if isinstance(raw_path, str) and raw_path.strip():
            try:
                search_root = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
            except ValueError as exc:
                return build_tool_error(
                    summary="查找文件失败",
                    error_code="path_outside_project",
                    error_message=str(exc),
                    tool_name=self.definition.name,
                )
        if not search_root.exists():
            return build_tool_error(
                summary="查找文件失败",
                error_code="path_not_found",
                error_message=f"搜索根路径不存在: {search_root}",
                tool_name=self.definition.name,
            )
        if search_root.is_file():
            return build_tool_error(
                summary="查找文件失败",
                error_code="invalid_search_root",
                error_message="glob 的 path 必须是目录，不能是文件。",
                tool_name=self.definition.name,
            )

        try:
            matches = [
                path.resolve()
                for path in search_root.glob(pattern)
                if path.is_file() and not is_skipped_path(path, context.project_root or context.cwd)
            ]
        except Exception as exc:
            return build_tool_error(
                summary="查找文件失败",
                error_code="glob_error",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        matches.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        relative_paths = [relative_display_path(path, context.cwd) for path in matches]
        model_paths = relative_paths[:MODEL_PATH_LIMIT]
        content_lines = [
            f"搜索根目录: {search_root}",
            f"模式: {pattern}",
            f"匹配文件数: {len(matches)}",
            "文件列表:",
        ]
        content_lines.extend(model_paths)
        content = _truncate_text("\n".join(content_lines))

        return build_tool_success(
            summary=f"找到 {len(matches)} 个文件",
            content=content,
            metadata={
                "pattern": pattern,
                "search_root": str(search_root),
                "paths": relative_paths[:UI_PATH_LIMIT],
                "total_matches": len(matches),
                "truncated": len(relative_paths) > UI_PATH_LIMIT,
                "truncated_for_ui": len(relative_paths) > UI_PATH_LIMIT,
                "truncated_for_model": len(relative_paths) > MODEL_PATH_LIMIT,
            },
            tool_name=self.definition.name,
        )


def _truncate_text(text: str) -> str:
    if len(text) <= MODEL_TEXT_CHAR_LIMIT:
        return text
    return text[:MODEL_TEXT_CHAR_LIMIT] + "\n... [结果已截断]"


FindFilesTool = GlobTool
