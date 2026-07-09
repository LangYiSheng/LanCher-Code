from __future__ import annotations

from pathlib import Path

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.common import relative_display_path, resolve_path_in_root

EDIT_FILE_DESCRIPTION = (
    "在文件中按原文做唯一匹配替换，适合局部修改代码或配置。"
    "应该在已经读过目标文件、并且确认文件没有被外部改动后使用。"
    "不要拿它做整文件重写；整文件改写请使用 write_file。"
)


class EditFileTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit_file",
            description=EDIT_FILE_DESCRIPTION,
            params_model={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要修改的文件路径。支持相对路径和绝对路径。",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "文件中要被替换的原始文本，必须唯一匹配。",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "替换后的新文本。",
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            category="write",
            is_concurrency_safe=False,
            allowed_modes=("default", "acceptEdits", "bypass"),
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        raw_path = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return build_tool_error(
                summary="改文件失败",
                error_code="invalid_arguments",
                error_message="path 必须是非空字符串。",
                tool_name=self.definition.name,
            )
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return build_tool_error(
                summary="改文件失败",
                error_code="invalid_arguments",
                error_message="old_text 和 new_text 都必须是字符串。",
                tool_name=self.definition.name,
            )

        try:
            path = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
        except ValueError as exc:
            return build_tool_error(
                summary="改文件失败",
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

        cached_content, guard_error = _guard_existing_file_edit(path, context)
        if guard_error is not None:
            return guard_error
        assert cached_content is not None

        match_count = cached_content.count(old_text)
        if match_count == 0:
            return build_tool_error(
                summary="唯一匹配替换失败",
                error_code="match_not_found",
                error_message="old_text 在文件中没有找到，请重新读取文件并提供精确原文。",
                metadata={"path": str(path), "match_count": 0},
                tool_name=self.definition.name,
            )
        if match_count > 1:
            return build_tool_error(
                summary="唯一匹配替换失败",
                error_code="match_not_unique",
                error_message=f"old_text 在文件中匹配到 {match_count} 次，不唯一，请提供更精确的原文片段。",
                metadata={"path": str(path), "match_count": match_count},
                tool_name=self.definition.name,
            )

        match_start = cached_content.index(old_text)
        line_start = cached_content[:match_start].count("\n") + 1
        updated = cached_content.replace(old_text, new_text, 1)
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return build_tool_error(
                summary=f"写入文件失败: {path}",
                error_code="write_error",
                error_message=str(exc),
                tool_name=self.definition.name,
            )

        context.file_state_cache.record_write(path, mtime_ns=path.stat().st_mtime_ns)
        display_lines = _build_display_lines(old_text, new_text, line_start=line_start)
        return build_tool_success(
            summary=f"已修改文件 {path.name}",
            content=(
                f"已修改文件 {path}\n"
                f"命中次数: {match_count}\n"
                f"起始行号: {line_start}"
            ),
            metadata={
                "path": str(path),
                "relative_path": relative_display_path(path, context.cwd),
                "match_count": match_count,
                "line_start": line_start,
                "display_lines": display_lines,
            },
            tool_name=self.definition.name,
        )


def _guard_existing_file_edit(path: Path, context: ToolContext) -> tuple[str | None, ToolExecutionResult | None]:
    state = context.file_state_cache.get(path)
    if state is None or not state.was_read or state.content is None:
        return None, build_tool_error(
            summary="改文件失败",
            error_code="stale_file_state",
            error_message="为防止基于过期内容改文件，请先使用 read_file 读取目标文件。",
            metadata={"path": str(path)},
            tool_name="edit_file",
        )
    current_mtime = path.stat().st_mtime_ns
    if state.mtime_ns != current_mtime:
        return None, build_tool_error(
            summary="改文件失败",
            error_code="file_changed_since_read",
            error_message="文件在读取后已被修改，请重新使用 read_file 读取最新内容后再编辑。",
            metadata={"path": str(path), "cached_mtime_ns": state.mtime_ns, "current_mtime_ns": current_mtime},
            tool_name="edit_file",
        )
    return state.content, None


def _build_display_lines(old_text: str, new_text: str, *, line_start: int) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    old_lines = old_text.splitlines() or [old_text]
    new_lines = new_text.splitlines() or [new_text]
    for index, line in enumerate(old_lines):
        lines.append({"text": f"- {line_start + index}\t{line}", "tone": "error"})
    for index, line in enumerate(new_lines):
        lines.append({"text": f"+ {line_start + index}\t{line}", "tone": "success"})
    return lines


ReplaceInFileTool = EditFileTool
