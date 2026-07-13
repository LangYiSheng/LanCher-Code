from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml

from lancher_code.errors import LanCherError
from lancher_code.models import (
    PermissionDecision,
    PermissionRequest,
    PermissionResolution,
    PermissionRule,
    RuleScope,
    RuntimeMode,
    ToolCall,
    ToolContext,
    ToolDefinition,
)
from lancher_code.tools.core.common import ensure_path_in_root, relative_display_path, resolve_path_in_root

PermissionRuleMatcher = Literal["exact", "glob"]

TOOL_LABELS: dict[str, str] = {
    "bash": "Bash",
    "read_file": "ReadFile",
    "write_file": "WriteFile",
    "edit_file": "EditFile",
    "glob": "Glob",
    "grep": "Grep",
    "write_plan_file": "WritePlanFile",
}
LABEL_TO_TOOL = {label.casefold(): tool_name for tool_name, label in TOOL_LABELS.items()}
PLAN_ALLOWED_PREFIXES = (
    "get-childitem",
    "ls",
    "dir",
    "pwd",
    "get-location",
    "get-content",
    "type",
    "cat",
    "rg",
    "select-string",
    "git status",
    "git diff",
    "where",
    "python --version",
    "python -v",
    "uv --version",
)
PLAN_BLOCKED_PATTERNS = (
    ">>",
    ">",
    "<",
    "|",
    "&&",
    "||",
    ";",
    "set-content",
    "add-content",
    "out-file",
    "remove-item",
    "move-item",
    "copy-item",
    "new-item",
    "rename-item",
    "start-process",
    "git checkout",
    "git commit",
    "git apply",
    "git cherry-pick",
    "npm ",
    "pnpm ",
    "yarn ",
    "pip ",
    "uv run",
    "uv sync",
)
COMMAND_BLACKLIST_PATTERNS = (
    re.compile(r"(^|[;&|])\s*(remove-item|del|erase|rm)\b", re.IGNORECASE),
    re.compile(r"(^|[;&|])\s*(shutdown|restart-computer|stop-computer)\b", re.IGNORECASE),
    re.compile(r"\b(format|diskpart|cipher)\b", re.IGNORECASE),
    re.compile(r"\b(runas|sudo)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-fdx?|checkout\s+--|restore\s+--source=)", re.IGNORECASE),
    re.compile(r"(?:^|[^<])>>?(?:[^>]|$)"),
)


class PermissionRuleFileError(LanCherError):
    pass


@dataclass(slots=True)
class PermissionCheck:
    decision: PermissionDecision
    reason_code: str | None = None
    reason_message: str | None = None
    request: PermissionRequest | None = None
    metadata: dict[str, object] | None = None


@dataclass(slots=True)
class _MatchTarget:
    tool_name: str
    tool_label: str
    value: str
    matcher: PermissionRuleMatcher


class PermissionStorage:
    def __init__(
        self,
        *,
        project_rules_path: Path | None = None,
        user_rules_path: Path | None = None,
    ) -> None:
        self._project_rules_path = project_rules_path
        self._user_rules_path = user_rules_path
        self._session_rules: list[PermissionRule] = []
        self._project_rules = self._load_rules(project_rules_path, "project")
        self._user_rules = self._load_rules(user_rules_path, "user")

    @property
    def project_rules_path(self) -> Path | None:
        return self._project_rules_path

    def rules_for_scope(self, scope: RuleScope) -> list[PermissionRule]:
        if scope == "session":
            return list(self._session_rules)
        if scope == "project":
            return list(self._project_rules)
        return list(self._user_rules)

    def add_session_rule(self, match: str, result: Literal["allow", "deny"]) -> PermissionRule:
        rule = PermissionRule(match=match, result=result, scope="session")
        self._session_rules.append(rule)
        return rule

    def add_project_rule(self, match: str, result: Literal["allow", "deny"]) -> PermissionRule:
        if self._project_rules_path is None:
            raise PermissionRuleFileError("当前会话没有配置项目级权限规则文件路径。")
        rule = PermissionRule(match=match, result=result, scope="project")
        self._project_rules.append(rule)
        self._write_rules(self._project_rules_path, self._project_rules)
        return rule

    @staticmethod
    def _load_rules(path: Path | None, scope: RuleScope) -> list[PermissionRule]:
        if path is None or not path.exists():
            return []
        try:
            raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PermissionRuleFileError(f"权限规则文件不是合法的 YAML: {path}") from exc
        except OSError as exc:
            raise PermissionRuleFileError(f"无法读取权限规则文件: {path}") from exc

        if raw_data is None:
            return []
        if not isinstance(raw_data, dict):
            raise PermissionRuleFileError(f"权限规则文件顶层必须是对象: {path}")

        raw_rules = raw_data.get("rules", [])
        if raw_rules is None:
            return []
        if not isinstance(raw_rules, list):
            raise PermissionRuleFileError(f"权限规则文件中的 rules 必须是数组: {path}")

        rules: list[PermissionRule] = []
        for index, item in enumerate(raw_rules, start=1):
            if not isinstance(item, dict):
                raise PermissionRuleFileError(f"权限规则第 {index} 项必须是对象: {path}")
            match = item.get("match")
            result = item.get("result")
            if not isinstance(match, str) or not match.strip():
                raise PermissionRuleFileError(f"权限规则第 {index} 项缺少合法的 match: {path}")
            if result not in {"allow", "deny"}:
                raise PermissionRuleFileError(f"权限规则第 {index} 项的 result 只能是 allow 或 deny: {path}")
            rules.append(PermissionRule(match=match.strip(), result=result, scope=scope))
        return rules

    @staticmethod
    def _write_rules(path: Path, rules: list[PermissionRule]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "rules": [
                {
                    "match": rule.match,
                    "result": rule.result,
                }
                for rule in rules
            ]
        }
        yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        path.write_text(yaml_text, encoding="utf-8")


class PermissionEngine:
    def __init__(self, storage: PermissionStorage | None = None) -> None:
        self._storage = storage or PermissionStorage()

    def evaluate(
        self,
        *,
        call: ToolCall,
        tool: ToolDefinition,
        context: ToolContext,
    ) -> PermissionCheck:
        try:
            target = self._build_match_target(call, tool, context)
        except ValueError as exc:
            return PermissionCheck(
                decision="deny",
                reason_code="path_outside_project",
                reason_message=str(exc),
                metadata={"mode": context.mode, "tool_name": tool.name},
            )
        metadata = self._build_denied_metadata(call, tool, context, target)

        if tool.name == "bash":
            blacklist_message = _match_command_blacklist(target.value)
            if blacklist_message is not None:
                return PermissionCheck(
                    decision="deny",
                    reason_code="permission_blacklist_denied",
                    reason_message=blacklist_message,
                    metadata=metadata,
                )
            if context.mode == "plan":
                plan_rejection = validate_plan_command(target.value)
                if plan_rejection is not None:
                    return PermissionCheck(
                        decision="deny",
                        reason_code="plan_mode_command_rejected",
                        reason_message=plan_rejection,
                        metadata=metadata,
                    )

        matched_rule = self._match_rules(target)
        if matched_rule is not None:
            return PermissionCheck(
                decision=matched_rule.result,
                reason_code=f"permission_rule_{matched_rule.result}",
                reason_message=f"命中 {matched_rule.scope} 级权限规则: {matched_rule.match}",
                metadata={**metadata, "rule_match": matched_rule.match, "rule_scope": matched_rule.scope},
            )

        mode_decision = self._mode_decision(tool, context.mode)
        if mode_decision == "allow":
            return PermissionCheck(decision="allow", metadata=metadata)
        if mode_decision == "deny":
            return PermissionCheck(
                decision="deny",
                reason_code="permission_mode_denied",
                reason_message=f"当前模式 {context.mode} 不允许执行该工具。",
                metadata=metadata,
            )

        return PermissionCheck(decision="ask", request=self._build_permission_request(call, tool, context, target))

    def apply_resolution(self, request: PermissionRequest, resolution: PermissionResolution) -> None:
        if resolution.outcome == "allow_session" and request.session_rule:
            self._storage.add_session_rule(request.session_rule, "allow")
        elif resolution.outcome == "allow_project" and request.project_rule:
            self._storage.add_project_rule(request.project_rule, "allow")

    def _match_rules(self, target: _MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            matched: PermissionRule | None = None
            rules = self._storage.rules_for_scope(scope)  # type: ignore[arg-type]
            for rule in rules:
                if _rule_matches(rule.match, target):
                    matched = rule
            if matched is not None:
                return matched
        return None

    @staticmethod
    def _mode_decision(tool: ToolDefinition, mode: RuntimeMode) -> PermissionDecision:
        if mode == "bypass":
            return "allow"
        if tool.category == "read":
            return "allow"
        if tool.permission is not None and tool.permission.source == "external":
            return "deny" if mode == "plan" else "ask"
        if mode == "acceptEdits" and tool.category == "write" and tool.name != "bash":
            return "allow"
        return "ask"

    def _build_match_target(self, call: ToolCall, tool: ToolDefinition, context: ToolContext) -> _MatchTarget:
        if tool.permission is not None and tool.permission.source == "external":
            return _MatchTarget(
                tool_name=tool.permission.rule_key,
                tool_label=tool.permission.display_name,
                value="",
                matcher="exact",
            )
        if tool.name == "bash":
            command = str(call.arguments.get("command", "")).strip()
            normalized = _normalize_command(command)
            return _MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=normalized, matcher="glob")
        if tool.name == "write_plan_file":
            if context.plan_file_path is None:
                relative_path = ".lancher/plan.md"
            else:
                plan_path = ensure_path_in_root(context.plan_file_path, context.project_root or context.cwd)
                relative_path = relative_display_path(plan_path, context.project_root or context.cwd)
            return _MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path), matcher="glob")
        if tool.name in {"read_file", "write_file", "edit_file"}:
            raw_path = str(call.arguments.get("path", "")).strip()
            resolved = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
            relative_path = relative_display_path(resolved, context.project_root or context.cwd)
            return _MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path), matcher="glob")
        if tool.name == "glob":
            pattern = str(call.arguments.get("pattern", "")).strip()
            return _MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=pattern.casefold(), matcher="glob")
        if tool.name == "grep":
            raw_path = call.arguments.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                resolved = resolve_path_in_root(context.cwd, raw_path, context.project_root or context.cwd)
                relative_path = relative_display_path(resolved, context.project_root or context.cwd)
            else:
                relative_path = "."
            return _MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=_normalize_path(relative_path), matcher="glob")
        return _MatchTarget(tool_name=tool.name, tool_label=tool.name, value="", matcher="exact")

    def _build_permission_request(
        self,
        call: ToolCall,
        tool: ToolDefinition,
        context: ToolContext,
        target: _MatchTarget,
    ) -> PermissionRequest:
        request_id = f"perm-{uuid4().hex[:8]}"
        if tool.permission is not None and tool.permission.source == "external":
            arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
            if len(arguments) > 1000:
                arguments = f"{arguments[:997]}..."
            rule = tool.permission.rule_key
            return PermissionRequest(
                request_id=request_id,
                call_id=call.call_id,
                tool_name=tool.name,
                tool_label=tool.permission.display_name,
                kind="external_tool",
                mode=context.mode,
                title="是否允许调用 MCP 工具",
                prompt=f"{tool.permission.server_name}/{tool.permission.remote_tool_name} 可能产生远程副作用。",
                details=f"参数: {arguments}",
                session_rule=rule,
                project_rule=rule,
                metadata={
                    "mode": context.mode,
                    "server": tool.permission.server_name or "",
                    "remote_tool": tool.permission.remote_tool_name or "",
                },
            )
        if tool.name == "bash":
            command = str(call.arguments.get("command", "")).strip()
            description = str(call.arguments.get("description", "")).strip()
            suggested_rule = f"{target.tool_label}({command})"
            normalized_rule = f"{target.tool_label}({command})"
            return PermissionRequest(
                request_id=request_id,
                call_id=call.call_id,
                tool_name=tool.name,
                tool_label=target.tool_label,
                kind="command",
                mode=context.mode,
                title="是否允许执行此命令",
                prompt="命令执行需要授权。",
                details=f"命令: {command}\n描述: {description or '(无描述)'}",
                command=command,
                description=description,
                session_rule=_suggest_rule(target.tool_label, command),
                project_rule=_suggest_rule(target.tool_label, command),
                metadata={"mode": context.mode},
            )

        file_paths = _request_file_paths(tool.name, call.arguments, context)
        preview_lines = _build_preview_lines(tool.name, call.arguments, context)
        return PermissionRequest(
            request_id=request_id,
            call_id=call.call_id,
            tool_name=tool.name,
            tool_label=target.tool_label,
            kind="file_edit",
            mode=context.mode,
            title="是否允许编辑此文件",
            prompt="文件写入需要授权。",
            details="\n".join(file_paths),
            file_paths=file_paths,
            preview_lines=preview_lines,
            metadata={"mode": context.mode},
        )

    def _build_denied_metadata(
        self,
        call: ToolCall,
        tool: ToolDefinition,
        context: ToolContext,
        target: _MatchTarget,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "mode": context.mode,
            "tool_name": tool.name,
            "tool_label": target.tool_label,
        }
        if tool.name == "bash":
            metadata["command"] = str(call.arguments.get("command", "")).strip()
            metadata["description"] = str(call.arguments.get("description", "")).strip()
        elif tool.name in {"read_file", "write_file", "edit_file", "write_plan_file"}:
            try:
                metadata["paths"] = _request_file_paths(tool.name, call.arguments, context)
            except ValueError:
                metadata["paths"] = []
            preview_lines = _build_preview_lines(tool.name, call.arguments, context)
            if preview_lines:
                metadata["display_lines"] = preview_lines
        return metadata


def validate_plan_command(command: str) -> str | None:
    lowered = _normalize_command(command)
    for pattern in PLAN_BLOCKED_PATTERNS:
        if pattern in lowered:
            return "Plan 模式下只允许只读命令，当前命令包含潜在副作用或旁路写入能力。"
    if any(lowered.startswith(prefix) for prefix in PLAN_ALLOWED_PREFIXES):
        return None
    return "Plan 模式下仅允许目录查看、文本搜索、git 状态或差异、解释器版本查询等只读命令。"


def _match_command_blacklist(command: str) -> str | None:
    for pattern in COMMAND_BLACKLIST_PATTERNS:
        if pattern.search(command):
            return "命中不可绕过的危险命令黑名单，已拒绝执行。"
    return None


def _rule_matches(rule_match: str, target: _MatchTarget) -> bool:
    parsed = _parse_rule(rule_match)
    if parsed is None:
        if target.value:
            return False
        return fnmatch.fnmatchcase(target.tool_name, rule_match.strip())
    tool_name, rule_value = parsed
    if tool_name != target.tool_name:
        return False
    candidate = target.value
    normalized_rule = _normalize_rule_value(target.tool_name, rule_value)
    if _has_glob(normalized_rule) or target.matcher == "glob":
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
    if tool_name == "bash":
        return _normalize_command(value)
    if tool_name in {"read_file", "write_file", "edit_file", "write_plan_file", "grep"}:
        return _normalize_path(value)
    return value.casefold()


def _normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip().casefold())


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip().casefold()
    return normalized or "."


def _has_glob(value: str) -> bool:
    return any(token in value for token in ("*", "?", "["))


def _suggest_rule(tool_label: str, command: str) -> str:
    normalized = re.sub(r"\s+", " ", command.strip())
    if " " not in normalized:
        return f"{tool_label}({normalized})"
    first_token, _rest = normalized.split(" ", 1)
    return f"{tool_label}({first_token} *)"


def _request_file_paths(tool_name: str, arguments: dict[str, object], context: ToolContext) -> list[str]:
    project_root = context.project_root or context.cwd
    if tool_name == "write_plan_file":
        if context.plan_file_path is None:
            return []
        path = ensure_path_in_root(context.plan_file_path, project_root)
        return [relative_display_path(path, project_root)]
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return []
    path = resolve_path_in_root(context.cwd, raw_path, project_root)
    return [relative_display_path(path, project_root)]


def _build_preview_lines(tool_name: str, arguments: dict[str, object], context: ToolContext) -> list[dict[str, str]]:
    if tool_name == "edit_file":
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return []
        preview: list[dict[str, str]] = []
        for line in old_text.splitlines()[:10] or [old_text]:
            preview.append({"text": f"- {line}", "tone": "error"})
        for line in new_text.splitlines()[:10] or [new_text]:
            preview.append({"text": f"+ {line}", "tone": "success"})
        return preview
    if tool_name in {"write_file", "write_plan_file"}:
        content = arguments.get("content")
        if not isinstance(content, str):
            return []
        preview = [{"text": f"+ {line}", "tone": "success"} for line in content.splitlines()[:10]]
        if not preview and content == "":
            preview.append({"text": "+ ", "tone": "success"})
        return preview
    return []
