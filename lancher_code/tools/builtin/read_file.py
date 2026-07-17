from __future__ import annotations

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.common import relative_display_path, resolve_path_in_root

DEFAULT_MAX_INLINE_LINES = 400
MAX_CHUNK_LINES = 400

READ_FILE_DESCRIPTION = (
    "读取文本文件内容。适合在准备修改文件、理解代码、核对配置、查看命令输出落盘结果时使用。"
    "如果文件很大，请使用 offset 和 limit 按行分页读取。"
    "不要用它搜索整个项目，也不要用它覆盖写文件；那些分别应该使用 glob/grep 或 write_file/edit_file。"
)


class ReadFileTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description=READ_FILE_DESCRIPTION,
            params_model={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件路径。支持相对路径和绝对路径。",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "可选。按行读取时的起始偏移，从 0 开始。",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_CHUNK_LINES,
                        "description": f"可选。最多返回多少行，单次上限 {MAX_CHUNK_LINES} 行。",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            category="read",
            is_concurrency_safe=True,
            allowed_modes=("default", "plan", "acceptEdits", "bypass"),
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        raw_path = arguments.get("path")
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit")

        if not isinstance(raw_path, str) or not raw_path.strip():
            return build_tool_error(
                summary="读取文件失败",
                error_code="invalid_arguments",
                error_message="path 必须是非空字符串。",
                tool_name=self.definition.name,
            )
        if not isinstance(offset, int) or offset < 0:
            return build_tool_error(
                summary="读取文件失败",
                error_code="invalid_arguments",
                error_message="offset 必须是大于等于 0 的整数。",
                tool_name=self.definition.name,
            )
        if limit is not None and (not isinstance(limit, int) or limit <= 0 or limit > MAX_CHUNK_LINES):
            return build_tool_error(
                summary="读取文件失败",
                error_code="invalid_arguments",
                error_message=f"limit 必须是 1 到 {MAX_CHUNK_LINES} 之间的整数。",
                tool_name=self.definition.name,
            )

        try:
            path = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
        except ValueError as exc:
            return build_tool_error(
                summary="读取文件失败",
                error_code="path_outside_project",
                error_message=str(exc),
                tool_name=self.definition.name,
            )
        if not path.exists():
            return build_tool_error(
                summary=f"文件不存在: {path}",
                error_code="file_not_found",
                error_message=f"文件不存在: {path}",
                tool_name=self.definition.name,
            )
        if not path.is_file():
            return build_tool_error(
                summary=f"路径不是文件: {path}",
                error_code="not_a_file",
                error_message=f"路径不是文件: {path}",
                tool_name=self.definition.name,
            )

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return build_tool_error(
                summary=f"无法按 UTF-8 读取文件: {path}",
                error_code="decode_error",
                error_message=f"无法按 UTF-8 读取文件: {path}",
                tool_name=self.definition.name,
            )
        except OSError as exc:
            return build_tool_error(
                summary=f"读取文件失败: {path}",
                error_code="read_error",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        lines = text.splitlines()
        total_lines = len(lines)
        if total_lines > DEFAULT_MAX_INLINE_LINES and limit is None:
            return build_tool_error(
                summary="大文件需要分页读取",
                error_code="large_file_requires_paging",
                error_message=(
                    f"文件共有 {total_lines} 行，超过单次直接读取上限。"
                    f"请重新调用 read_file，并显式提供 offset 和 limit（limit 不超过 {MAX_CHUNK_LINES}）。"
                ),
                metadata={
                    "path": str(path),
                    "relative_path": relative_display_path(path, context.cwd),
                    "total_lines": total_lines,
                },
                tool_name=self.definition.name,
            )

        effective_limit = total_lines if limit is None else limit
        end = min(offset + effective_limit, total_lines)
        selected_lines = lines[offset:end]
        source_content = "".join(text.splitlines(keepends=True)[offset:end])
        numbered = _format_numbered_lines(selected_lines, start_line=offset + 1)
        is_complete = offset == 0 and end >= total_lines

        context.file_state_cache.record_read(
            path,
            text,
            mtime_ns=path.stat().st_mtime_ns,
            is_complete=is_complete,
        )

        body = numbered if selected_lines else "(该范围内没有内容)"
        content = (
            f"文件: {path}\n"
            f"总行数: {total_lines}\n"
            f"返回范围: {offset + 1}-{max(offset, end)}\n"
            f"{body}"
        )
        return build_tool_success(
            summary=f"已读取文件 {path.name}",
            content=content,
            metadata={
                "path": str(path),
                "relative_path": relative_display_path(path, context.cwd),
                "total_lines": total_lines,
                "line_start": offset + 1,
                "line_end": end,
                "returned_lines": len(selected_lines),
                "is_complete": is_complete,
                "normalized_path": str(path.resolve()),
                "source_content": source_content,
            },
            tool_name=self.definition.name,
        )


def _format_numbered_lines(lines: list[str], *, start_line: int) -> str:
    if not lines:
        return ""
    return "\n".join(f"{start_line + index}\t{line}" for index, line in enumerate(lines))
