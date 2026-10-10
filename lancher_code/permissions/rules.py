from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from lancher_code.permissions.models import PermissionMatchKind
from lancher_code.contracts.tools import ToolCall, ToolDefinition
from lancher_code.tools.context import ToolContext
from lancher_code.filesystem.access import PathWriteDeniedError, ensure_path_in_root, relative_display_path, resolve_path_in_root


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
}


LABEL_TO_TOOL = {label.casefold(): tool_name for tool_name, label in TOOL_LABELS.items()}


COMMAND_BLACKLIST_PATTERNS = (
    re.compile(r"(^|[;&|])\s*(remove-item|del|erase|rm)\b", re.IGNORECASE),
    re.compile(r"(^|[;&|])\s*(shutdown|restart-computer|stop-computer)\b", re.IGNORECASE),
    re.compile(r"\b(format|diskpart|cipher)\b", re.IGNORECASE),
    re.compile(r"\b(runas|sudo)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-fdx?|checkout\s+--|restore\s+--source=)", re.IGNORECASE),
    re.compile(r"(?:^|[^<])>>?(?:[^>]|$)"),
)


@dataclass(slots=True)
class MatchTarget:
    tool_name: str
    tool_label: str
    value: str
    exact_value: str | None = None


def match_command_blacklist(command: str) -> str | None:
    for pattern in COMMAND_BLACKLIST_PATTERNS:
        if pattern.search(command):
            return "命中不可绕过的危险命令黑名单，已拒绝执行。"
    return None


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
        return _normalize_command(value)
    if tool_name in {"read_file", "write_file", "edit_file", "write_plan_file", "grep"}:
        return _normalize_path(value)
    return value.casefold()


def _normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip().casefold())


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
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str) or not raw_cwd.strip():
                raise ValueError("cwd 必须是项目内的非空路径字符串。")
            resolve_path_in_root(context.cwd, raw_cwd, context.project_root or context.cwd)
        normalized = _normalize_command(command)
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=normalized, exact_value=command)
    if tool.name in {"process_write", "process_background"}:
        process_id = str(call.arguments.get("process_id", "")).strip()
        return MatchTarget(tool.name, TOOL_LABELS[tool.name], process_id)
    if tool.name == "write_plan_file":
        if context.plan_file_path is None:
            raise PathWriteDeniedError("missing_plan_file_path", "当前上下文没有会话计划文件路径。")
        else:
            plan_path = ensure_path_in_root(context.plan_file_path, context.project_root or context.cwd)
            relative_path = relative_display_path(plan_path, context.project_root or context.cwd)
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path),)
    if tool.name in {"read_file", "write_file", "edit_file"}:
        raw_path = str(call.arguments.get("path", "")).strip()
        resolved = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
        relative_path = relative_display_path(resolved, context.project_root or context.cwd)
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path),)
    if tool.name == "glob":
        pattern = str(call.arguments.get("pattern", "")).strip()
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=pattern.casefold(),)
    if tool.name == "grep":
        raw_path = call.arguments.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            resolved = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
            relative_path = relative_display_path(resolved, context.project_root or context.cwd)
        else:
            relative_path = "."
        return MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path),)
    return MatchTarget(tool_name=tool.name, tool_label=tool.name, value="",)
