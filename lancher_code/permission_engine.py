from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
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
    PermissionPolicy,
    PermissionMatchKind,
    ToolCall,
    ToolContext,
    ToolDefinition,
    tool_available_in_phase,
)
from lancher_code.tools.core.common import (
    PathWriteDeniedError, ensure_path_in_root, ensure_writable_path,
    is_session_workspace_path, relative_display_path, resolve_path_in_root,
)

PermissionRuleMatcher = Literal["exact", "glob"]

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
    exact_value: str | None = None


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
        self._session_rule_callbacks: list[Callable[[], None]] = []
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

    @property
    def user_rules_path(self) -> Path | None:
        return self._user_rules_path

    def replace_rules(
        self,
        scope: Literal["project", "user"],
        rules: list[PermissionRule],
        *,
        persist: bool = True,
    ) -> None:
        """替换指定持久化层的规则；会话级规则保持不变。"""
        path = self._project_rules_path if scope == "project" else self._user_rules_path
        if path is None:
            raise PermissionRuleFileError(f"{scope} 权限规则文件路径未配置。")
        normalized = [
            PermissionRule(match=rule.match, result=rule.result, scope=scope, match_kind=rule.match_kind)
            for rule in rules
        ]
        for rule in normalized:
            _validate_match_kind(rule.match_kind)
        if persist:
            self._write_rules(path, normalized)
        if scope == "project":
            self._project_rules = normalized
        else:
            self._user_rules = normalized

    def add_session_rule(self, match: str, result: Literal["allow", "deny"], *, match_kind: PermissionMatchKind = "legacy") -> PermissionRule:
        normalized_match = match.strip()
        if not normalized_match:
            raise ValueError("session 权限规则的 match 不能为空。")
        _validate_match_kind(match_kind)
        rule = PermissionRule(match=normalized_match, result=result, scope="session", match_kind=match_kind)
        self._session_rules.append(rule)
        self._notify_session_rules_changed()
        return rule

    def replace_session_rules(
        self,
        rules: list[PermissionRule],
        *,
        notify: bool = True,
    ) -> None:
        normalized: list[PermissionRule] = []
        for rule in rules:
            match = rule.match.strip()
            if not match:
                raise ValueError("session 权限规则的 match 不能为空。")
            if rule.result not in {"allow", "deny"}:
                raise ValueError("session 权限规则的 result 只能是 allow 或 deny。")
            _validate_match_kind(rule.match_kind)
            normalized.append(PermissionRule(match=match, result=rule.result, scope="session", match_kind=rule.match_kind))
        self._session_rules = normalized
        if notify:
            self._notify_session_rules_changed()

    def subscribe_session_rules_changed(self, callback: Callable[[], None]) -> None:
        if callback not in self._session_rule_callbacks:
            self._session_rule_callbacks.append(callback)

    def _notify_session_rules_changed(self) -> None:
        for callback in tuple(self._session_rule_callbacks):
            callback()

    def add_project_rule(self, match: str, result: Literal["allow", "deny"], *, match_kind: PermissionMatchKind = "legacy") -> PermissionRule:
        if self._project_rules_path is None:
            raise PermissionRuleFileError("当前会话没有配置项目级权限规则文件路径。")
        _validate_match_kind(match_kind)
        rule = PermissionRule(match=match, result=result, scope="project", match_kind=match_kind)
        updated = [*self._project_rules, rule]
        self._write_rules(self._project_rules_path, updated)
        self._project_rules = updated
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
            match_kind = item.get("match_kind", "legacy")
            if match_kind not in {"exact", "glob", "legacy"}:
                raise PermissionRuleFileError(f"权限规则第 {index} 项的 match_kind 无效: {path}")
            rules.append(PermissionRule(match=match.strip(), result=result, scope=scope, match_kind=match_kind))
        return rules

    @staticmethod
    def _write_rules(path: Path, rules: list[PermissionRule]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "rules": [
                {
                    "match": rule.match,
                    "result": rule.result,
                    "match_kind": rule.match_kind,
                }
                for rule in rules
            ]
        }
        yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        path.write_text(yaml_text, encoding="utf-8")


class PermissionEngine:
    def __init__(self, storage: PermissionStorage | None = None) -> None:
        self._storage = storage or PermissionStorage()

    @property
    def storage(self) -> PermissionStorage:
        return self._storage

    def evaluate(
        self,
        *,
        call: ToolCall,
        tool: ToolDefinition,
        context: ToolContext,
    ) -> PermissionCheck:
        if not tool_available_in_phase(tool, context.work_phase):
            return PermissionCheck(
                decision="deny", reason_code="phase_disallowed",
                reason_message=f"当前 {context.work_phase} 阶段不允许执行该工具。",
                metadata={
                    "mode": context.mode, "cwd": str(context.cwd),
                    "work_phase": context.work_phase, "permission_policy": context.permission_policy,
                    "tool_name": tool.name,
                },
            )
        try:
            target = self._build_match_target(call, tool, context)
            write_path = self._write_target(call, tool, context)
            if write_path is not None:
                write_path = ensure_writable_path(write_path, context)
        except PathWriteDeniedError as exc:
            return PermissionCheck(
                decision="deny", reason_code=exc.reason_code, reason_message=str(exc),
                metadata={"work_phase": context.work_phase, "session_id": context.session_id, "tool_name": tool.name},
            )
        except ValueError as exc:
            return PermissionCheck(
                decision="deny",
                reason_code="path_outside_project",
                reason_message=str(exc),
                metadata={
                    "mode": context.mode, "cwd": str(context.cwd),
                    "work_phase": context.work_phase, "permission_policy": context.permission_policy,
                    "tool_name": tool.name,
                },
            )
        metadata = self._build_denied_metadata(call, tool, context, target)

        if tool.name == "run_command":
            blacklist_message = _match_command_blacklist(target.value)
            if blacklist_message is not None:
                return PermissionCheck(
                    decision="deny",
                    reason_code="permission_blacklist_denied",
                    reason_message=blacklist_message,
                    metadata=metadata,
                )
        if tool.name == "process_write":
            # stdin 也可能是交互 Shell 的命令入口；已有不可绕过的命令限制
            # 不能仅因为换成了输入通道就消失。
            text = str(call.arguments.get("text", ""))
            blocked = next((_match_command_blacklist(line) for line in text.splitlines()
                            if _match_command_blacklist(line) is not None), None)
            if blocked is not None:
                return PermissionCheck(decision="deny", reason_code="permission_blacklist_denied",
                                       reason_message=blocked, metadata=metadata)
        workspace_grant = write_path is not None and is_session_workspace_path(write_path, context)
        matched_rule = self._match_denied_rule(target) if workspace_grant else None
        matched_rule = matched_rule or self._match_rules(target)
        # 交互 Shell 的 stdin 可以执行新的任意命令。历史允许规则不能把
        # 一次命令批准扩大成一个永久的 Shell 输入通道，明确拒绝仍然生效。
        if tool.name in {"process_write", "process_background"}:
            denied_rule = self._match_denied_rule(target)
            if denied_rule is not None:
                return PermissionCheck(
                    decision="deny", reason_code="permission_rule_deny",
                    reason_message=f"命中 {denied_rule.scope} 级权限规则: {denied_rule.match}",
                    metadata=metadata,
                )
            if context.permission_policy != "bypass":
                return PermissionCheck(decision="ask", request=self._build_permission_request(call, tool, context, target))
        if matched_rule is not None:
            return PermissionCheck(
                decision=matched_rule.result,
                reason_code=f"permission_rule_{matched_rule.result}",
                reason_message=f"命中 {matched_rule.scope} 级权限规则: {matched_rule.match}",
                metadata={**metadata, "rule_match": matched_rule.match, "rule_scope": matched_rule.scope},
            )

        if workspace_grant:
            return PermissionCheck(
                decision="allow", reason_code="session_workspace_allowed",
                reason_message="已批准在当前会话工作目录中操作文件。",
                metadata={**metadata, "session_id": context.session_id},
            )

        mode_decision = self._policy_decision(tool, context.permission_policy)
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
            self._storage.add_session_rule(request.session_rule, "allow", match_kind=request.match_kind)
        elif resolution.outcome == "allow_project" and request.project_rule:
            self._storage.add_project_rule(request.project_rule, "allow", match_kind=request.match_kind)

    def _match_rules(self, target: _MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            matched: PermissionRule | None = None
            rules = self._storage.rules_for_scope(scope)  # type: ignore[arg-type]
            for rule in rules:
                if _rule_matches(rule.match, target, rule.match_kind):
                    matched = rule
            if matched is not None:
                return matched
        return None

    def _match_denied_rule(self, target: _MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            for rule in reversed(self._storage.rules_for_scope(scope)):
                if rule.result == "deny" and _rule_matches(rule.match, target, rule.match_kind):
                    return rule
        return None

    @staticmethod
    def _write_target(call: ToolCall, tool: ToolDefinition, context: ToolContext) -> Path | None:
        if tool.permission is not None and tool.permission.source == "external":
            return None
        if tool.name == "write_plan_file":
            if context.plan_file_path is None or not is_session_workspace_path(context.plan_file_path, context):
                raise PathWriteDeniedError("missing_plan_file_path", "当前上下文没有有效的会话计划文件路径。")
            return context.plan_file_path
        if tool.name in {"write_file", "edit_file"}:
            raw_path = str(call.arguments.get("path", "")).strip()
            path = Path(raw_path)
            return path if path.is_absolute() else context.cwd / path
        return None

    @staticmethod
    def _policy_decision(tool: ToolDefinition, policy: PermissionPolicy) -> PermissionDecision:
        if tool.name in {"process_list", "process_read", "process_wait", "process_stop"} and (tool.permission is None or tool.permission.source != "external"):
            return "allow"
        if policy == "bypass":
            return "allow"
        if tool.category == "read":
            return "allow"
        if tool.permission is not None and tool.permission.source == "external":
            return "ask"
        if policy == "acceptEdits" and tool.category == "write" and tool.name != "run_command":
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
        if tool.name == "run_command":
            command = str(call.arguments.get("command", "")).strip()
            raw_cwd = call.arguments.get("cwd")
            if raw_cwd is not None:
                if not isinstance(raw_cwd, str) or not raw_cwd.strip():
                    raise ValueError("cwd 必须是项目内的非空路径字符串。")
                resolve_path_in_root(context.cwd, raw_cwd, context.project_root or context.cwd)
            normalized = _normalize_command(command)
            return _MatchTarget(tool_name=tool.name, tool_label=TOOL_LABELS[tool.name], value=normalized, matcher="glob", exact_value=command)
        if tool.name in {"process_write", "process_background"}:
            process_id = str(call.arguments.get("process_id", "")).strip()
            return _MatchTarget(tool.name, TOOL_LABELS[tool.name], process_id, "exact")
        if tool.name == "write_plan_file":
            if context.plan_file_path is None:
                raise PathWriteDeniedError("missing_plan_file_path", "当前上下文没有会话计划文件路径。")
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
        metadata: dict[str, object] = {
            "mode": context.mode,
            "cwd": str(context.cwd),
            "work_phase": context.work_phase,
            "permission_policy": context.permission_policy,
        }
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
                work_phase=context.work_phase,
                permission_policy=context.permission_policy,
                title="是否允许调用 MCP 工具",
                prompt=f"{tool.permission.server_name}/{tool.permission.remote_tool_name} 可能产生远程副作用。",
                details=f"参数: {arguments}",
                session_rule=rule,
                project_rule=rule,
                metadata={
                    **metadata,
                    "server": tool.permission.server_name or "",
                    "remote_tool": tool.permission.remote_tool_name or "",
                },
            )
        if tool.name == "run_command":
            command = str(call.arguments.get("command", "")).strip()
            description = str(call.arguments.get("description", "")).strip()
            raw_cwd = call.arguments.get("cwd")
            command_cwd = resolve_path_in_root(context.cwd, raw_cwd, context.project_root or context.cwd) if isinstance(raw_cwd, str) and raw_cwd.strip() else context.cwd
            lifetime = call.arguments.get("lifetime", "turn")
            transport = call.arguments.get("transport", "pipe")
            yield_ms = call.arguments.get("yield_ms", 1000)
            maximum = call.arguments.get("max_runtime_ms")
            lifetime_label = "Session 后台：跨轮次和会话切换继续运行" if lifetime == "session" else "本轮任务：本轮结束或停止时收尾"
            exact_rule = f"{target.tool_label}({command})"
            return PermissionRequest(
                request_id=request_id,
                call_id=call.call_id,
                tool_name=tool.name,
                tool_label=target.tool_label,
                kind="command",
                mode=context.mode,
                work_phase=context.work_phase,
                permission_policy=context.permission_policy,
                title="是否允许执行此命令",
                prompt="命令执行需要授权。",
                details=(f"命令: {command}\n描述: {description or '(无描述)'}\n"
                         f"工作目录: {command_cwd}\n归属: {lifetime_label}\n终端: {transport}\n"
                         f"本次等待: {yield_ms} 毫秒\n运行上限: {maximum if maximum is not None else '未设置'}"),
                command=command,
                description=description,
                session_rule=exact_rule,
                project_rule=exact_rule,
                metadata={**metadata, "command_cwd": str(command_cwd), "lifetime": lifetime,
                          "transport": transport, "yield_ms": yield_ms, "max_runtime_ms": maximum},
            )

        if tool.name in {"process_write", "process_background"}:
            process_id = str(call.arguments.get("process_id", "")).strip()
            input_data = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
            return PermissionRequest(
                request_id=request_id, call_id=call.call_id, tool_name=tool.name,
                tool_label=target.tool_label, kind="command", mode=context.mode,
                work_phase=context.work_phase, permission_policy=context.permission_policy,
                title="是否允许向进程发送输入" if tool.name == "process_write" else "是否允许将进程转入后台",
                prompt="进程输入可能执行新的命令。" if tool.name == "process_write" else "进程将在本轮结束后继续运行。",
                details=f"进程: {process_id}\n参数: {input_data}",
                command=input_data, description=str(call.arguments.get("description", "")),
                session_rule=None, project_rule=None,
                metadata={**metadata, "process_id": process_id, "allow_once_only": True},
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
            work_phase=context.work_phase,
            permission_policy=context.permission_policy,
            title="是否允许编辑此文件",
            prompt="文件写入需要授权。",
            details="\n".join(file_paths),
            file_paths=file_paths,
            preview_lines=preview_lines,
            session_rule=f"{target.tool_label}({target.value})",
            project_rule=f"{target.tool_label}({target.value})",
            metadata=metadata,
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
            "cwd": str(context.cwd),
            "work_phase": context.work_phase,
            "permission_policy": context.permission_policy,
            "tool_name": tool.name,
            "tool_label": target.tool_label,
        }
        if tool.name == "run_command":
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


def _match_command_blacklist(command: str) -> str | None:
    for pattern in COMMAND_BLACKLIST_PATTERNS:
        if pattern.search(command):
            return "命中不可绕过的危险命令黑名单，已拒绝执行。"
    return None


def _validate_match_kind(match_kind: str) -> None:
    if not isinstance(match_kind, str) or match_kind not in {"exact", "glob", "legacy"}:
        raise ValueError("权限规则 match_kind 无效。")


def _rule_matches(rule_match: str, target: _MatchTarget, match_kind: PermissionMatchKind = "legacy") -> bool:
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
    if match_kind == "glob" or _has_glob(normalized_rule) or target.matcher == "glob":
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


def _has_glob(value: str) -> bool:
    return any(token in value for token in ("*", "?", "["))


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
