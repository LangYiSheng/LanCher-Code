from __future__ import annotations

import re
from pathlib import Path

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.tools.core.common import (
    MODEL_MATCH_LIMIT,
    MODEL_TEXT_CHAR_LIMIT,
    UI_PATH_LIMIT,
    iter_files,
    relative_display_path,
    resolve_path,
)

MAX_LINE_CHARS = 300

GREP_DESCRIPTION = (
    "在代码内容里按正则逐文件逐行搜索。"
    "适合查找符号定义、关键字、报错文本、配置项、日志格式或需要正则表达式匹配的内容。"
    "不要用它列目录，也不要用它读取整文件上下文；那些分别应使用 glob 和 read_file。"
    "pattern 是正则表达式；可选 path 限制搜索范围；可选 include 用文件名 glob 过滤，例如 **/*.py。"
    "工具会自动跳过二进制文件、无法解码的内容和常见缓存目录。content 会返回 file:line:content 的详细命中列表，metadata 会保留前 200 条供 UI 展示。"
)


class GrepTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="grep",
            description=GREP_DESCRIPTION,
            params_model={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式模式。",
                    },
                    "path": {
                        "type": "string",
                        "description": "可选。限制搜索根路径，支持目录或单个文件。",
                    },
                    "include": {
                        "type": "string",
                        "description": "可选。用 glob 过滤文件名，例如 **/*.py。",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            category="read",
            is_concurrency_safe=True,
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        pattern = arguments.get("pattern")
        raw_path = arguments.get("path")
        include = arguments.get("include")
        if not isinstance(pattern, str) or not pattern.strip():
            return build_tool_error(
                summary="搜索代码失败",
                error_code="invalid_arguments",
                error_message="pattern 必须是非空字符串。",
                tool_name=self.definition.name,
            )
        if include is not None and not isinstance(include, str):
            return build_tool_error(
                summary="搜索代码失败",
                error_code="invalid_arguments",
                error_message="include 必须是字符串。",
                tool_name=self.definition.name,
            )

        search_root = context.cwd
        if isinstance(raw_path, str) and raw_path.strip():
            search_root = resolve_path(context.cwd, raw_path)
        if not search_root.exists():
            return build_tool_error(
                summary="搜索代码失败",
                error_code="path_not_found",
                error_message=f"搜索根路径不存在: {search_root}",
                tool_name=self.definition.name,
            )

        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return build_tool_error(
                summary="搜索代码失败",
                error_code="invalid_regex",
                error_message=f"正则表达式无效: {exc}",
                tool_name=self.definition.name,
            )

        files = iter_files(search_root, include=include)
        files.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)

        match_lines: list[str] = []
        ui_matches: list[dict[str, object]] = []
        total_matches = 0

        for path in files:
            if _looks_binary(path):
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not regex.search(line):
                            continue
                        snippet = line.rstrip("\n")
                        if len(snippet) > MAX_LINE_CHARS:
                            snippet = snippet[:MAX_LINE_CHARS] + "... [结果已截断]"
                        display_path = relative_display_path(path, context.cwd)
                        line_text = f"{display_path}:{line_number}:{snippet}"
                        total_matches += 1
                        if len(match_lines) < MODEL_MATCH_LIMIT:
                            match_lines.append(line_text)
                        if len(ui_matches) < UI_PATH_LIMIT:
                            ui_matches.append(
                                {
                                    "path": display_path,
                                    "line": line_number,
                                    "content": snippet,
                                }
                            )
            except OSError:
                continue

        content_lines = [
            f"搜索根路径: {search_root}",
            f"正则: {pattern}",
            f"文件过滤: {include or '(无)'}",
            f"命中数: {total_matches}",
            "命中列表:",
        ]
        content_lines.extend(match_lines)
        content = _truncate_text("\n".join(content_lines))

        return build_tool_success(
            summary=f"找到 {total_matches} 条命中",
            content=content,
            metadata={
                "pattern": pattern,
                "search_root": str(search_root),
                "include": include,
                "matches": ui_matches,
                "total_matches": total_matches,
                "truncated": total_matches > UI_PATH_LIMIT,
                "truncated_for_ui": total_matches > UI_PATH_LIMIT,
                "truncated_for_model": total_matches > MODEL_MATCH_LIMIT,
            },
            tool_name=self.definition.name,
        )


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return True
    return b"\x00" in chunk


def _truncate_text(text: str) -> str:
    if len(text) <= MODEL_TEXT_CHAR_LIMIT:
        return text
    return text[:MODEL_TEXT_CHAR_LIMIT] + "\n... [结果已截断]"


SearchCodeTool = GrepTool
