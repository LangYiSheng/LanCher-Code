from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from dataclasses import dataclass
from lancher_code.permissions.models import PermissionMatchKind
from lancher_code.contracts.tools import ToolCall, ToolDefinition
from lancher_code.tools.context import ToolContext
from lancher_code.filesystem.access import PathWriteDeniedError, relative_display_path, resolve_access_path


TOOL_LABELS: dict[str, str] = {
    "run_command": "RunCommand",
    "process_write": "ProcessWrite",
    "process_background": "ProcessBackground",
    "read_file": "ReadFile",
    "write_file": "WriteFile",
    "edit_file": "EditFile",
    "glob": "Glob",
    "grep": "Grep",
    "write_plan_file": "WritePlanFile",
    "delete_file": "DeleteFile",
}


LABEL_TO_TOOL = {label.casefold(): tool_name for tool_name, label in TOOL_LABELS.items()}


@dataclass(slots=True)
class MatchTarget:
    tool_name: str
    tool_label: str
    value: str
    exact_value: str | None = None
    command_cwd: str | None = None
    command_shell: str | None = None
    command_name: str | None = None


def current_command_shell() -> str:
    """与实际进程后端保持一致，不根据命令正文猜测 Shell。"""
    return "powershell" if os.name == "nt" else "sh"


def rule_context_matches(rule, target: MatchTarget) -> bool:
    if rule.command_cwd is None:
        return True
    if target.command_cwd is None or target.command_shell != rule.command_shell:
        return False
    return os.path.normcase(os.path.normpath(rule.command_cwd)) == os.path.normcase(os.path.normpath(target.command_cwd))


def validate_match_kind(match_kind: str) -> None:
    if not isinstance(match_kind, str) or match_kind not in {"exact", "glob"}:
        raise ValueError("权限规则 match_kind 无效。")


def rule_matches(rule_match: str, target: MatchTarget, match_kind: PermissionMatchKind = "exact") -> bool:
    parsed = _parse_rule(rule_match)
    if parsed is None:
        if target.value and target.tool_name not in {"process_write", "process_background"}:
            return False
        if match_kind == "exact":
            return target.tool_name == rule_match.strip()
        return fnmatch.fnmatchcase(target.tool_name, rule_match.strip())
    tool_name, rule_value = parsed
    if tool_name != target.tool_name:
        return False
    if match_kind == "exact" and target.tool_name == "run_command":
        return (target.exact_value if target.exact_value is not None else target.value) == rule_value.strip()
    candidate = target.value
    normalized_rule = _normalize_rule_value(target.tool_name, rule_value)
    if match_kind == "exact":
        return candidate == normalized_rule
    if match_kind == "glob":
        if target.tool_name == "run_command" and target.command_shell == "powershell" and target.command_name:
            # 只规范化命令名称，不改变参数中的大小写、引号或空白。
            from lancher_code.permissions.shell import canonical_command_name
            head = re.match(r"([^\s]+)(.*)", normalized_rule, re.DOTALL)
            candidate_head = re.match(r"([^\s]+)(.*)", candidate, re.DOTALL)
            if head and candidate_head and not any(char in head[1] for char in "*?["):
                normalized_rule = canonical_command_name(head[1], "powershell") + head[2]
                candidate = target.command_name + candidate_head[2]
        return fnmatch.fnmatchcase(candidate, normalized_rule)
    return candidate == normalized_rule


def _parse_rule(rule_match: str) -> tuple[str, str] | None:
    text = rule_match.strip()
    if not text.endswith(")") or "(" not in text:
        return None
    open_paren = text.find("(")
    label = text[:open_paren].strip().casefold()
    pattern = text[open_paren + 1 : -1]
    tool_name = LABEL_TO_TOOL.get(label)
    if tool_name is None:
        return None
    return tool_name, pattern


def _normalize_rule_value(tool_name: str, value: str) -> str:
    if tool_name == "run_command":
        return value.strip()
    if tool_name in {"read_file", "write_file", "edit_file", "delete_file", "write_plan_file", "grep"}:
        return _normalize_path(value)
    return value.casefold()


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip().casefold()
    return normalized or "."


def build_match_target(call: ToolCall, tool: ToolDefinition, context: ToolContext) -> MatchTarget:
    if tool.permission is not None and tool.permission.source == "external":
        return MatchTarget(
            tool_name=tool.permission.rule_key,
            tool_label=tool.permission.display_name,
            value="",
        )
    if tool.name == "run_command":
        command = str(call.arguments.get("command", "")).strip()
        raw_cwd = call.arguments.get("cwd")
        command_cwd = resolve_access_path(context.cwd, ".", context)
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str) or not raw_cwd.strip():
                raise ValueError("cwd 必须是非空路径字符串。")
            command_cwd = resolve_access_path(context.cwd, raw_cwd, context)
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=command, exact_value=command,
                           command_cwd=str(command_cwd), command_shell=current_command_shell())
    if tool.name in {"process_write", "process_background"}:
        process_id = str(call.arguments.get("process_id", "")).strip()
        return MatchTarget(tool.name, TOOL_LABELS[tool.name], process_id)
    if tool.name == "write_plan_file":
        if context.plan_file_path is None:
            raise PathWriteDeniedError("missing_plan_file_path", "当前上下文没有会话计划文件路径。")
        else:
            plan_path = context.plan_file_path.resolve()
            relative_path = relative_display_path(plan_path, context.project_root or context.cwd)
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path),)
    if tool.name == "delete_file":
        from lancher_code.filesystem.deletion import resolve_delete_target
        path = resolve_delete_target(str(call.arguments.get("path", "")), context)
        try:
            relative_path = str(path.relative_to((context.project_root or context.cwd).resolve()))
        except ValueError:
            relative_path = str(path)
        return MatchTarget(tool.name, TOOL_LABELS[tool.name], _normalize_path(relative_path))
    if tool.name in {"read_file", "write_file", "edit_file"}:
        raw_path = str(call.arguments.get("path", "")).strip()
        resolved = resolve_access_path(context.cwd, raw_path, context)
        relative_path = relative_display_path(resolved, context.project_root or context.cwd)
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path),)
    if tool.name == "glob":
        pattern = str(call.arguments.get("pattern", "")).strip()
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=pattern.casefold(),)
    if tool.name == "grep":
        raw_path = call.arguments.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            resolved = resolve_access_path(context.cwd, raw_path, context)
            relative_path = relative_display_path(resolved, context.project_root or context.cwd)
        else:
            relative_path = "."
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path),)
    return MatchTarget(tool_name=tool.name, tool_label=tool.name, value="",)
