from __future__ import annotations

from pathlib import Path
from lancher_code.permissions.models import PermissionDecision, PermissionRequest, PermissionResolution, PermissionRule
from lancher_code.contracts.control import PermissionPolicy
from lancher_code.contracts.tools import ToolCall, ToolDefinition, tool_available_in_phase
from lancher_code.tools.context import ToolContext
from lancher_code.filesystem.access import PathWriteDeniedError, ensure_writable_path, is_session_workspace_path
from lancher_code.permissions.models import PermissionCheck
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.permissions.rules import MatchTarget
from lancher_code.permissions.rules import match_command_blacklist
from lancher_code.permissions.rules import rule_matches
from lancher_code.permissions.preview import build_denied_metadata
from lancher_code.permissions.rules import build_match_target
from lancher_code.permissions.preview import build_permission_request


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
                    "cwd": str(context.cwd),
                    "work_phase": context.work_phase, "permission_policy": context.permission_policy,
                    "tool_name": tool.name,
                },
            )
        try:
            target = build_match_target(call, tool, context)
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
                    "cwd": str(context.cwd),
                    "work_phase": context.work_phase, "permission_policy": context.permission_policy,
                    "tool_name": tool.name,
                },
            )
        metadata = build_denied_metadata(call, tool, context, target)

        if tool.name == "run_command":
            blacklist_message = match_command_blacklist(target.value)
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
            blocked = next((match_command_blacklist(line) for line in text.splitlines()
                            if match_command_blacklist(line) is not None), None)
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
                return PermissionCheck(decision="ask", request=build_permission_request(call, tool, context, target))
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

        policy_decision = self._policy_decision(tool, context.permission_policy)
        if policy_decision == "allow":
            return PermissionCheck(decision="allow", metadata=metadata)

        return PermissionCheck(decision="ask", request=build_permission_request(call, tool, context, target))

    def apply_resolution(self, request: PermissionRequest, resolution: PermissionResolution) -> None:
        if resolution.outcome == "allow_session" and request.session_rule:
            self._storage.add_session_rule(request.session_rule, "allow", match_kind=request.match_kind)
        elif resolution.outcome == "allow_project" and request.project_rule:
            self._storage.add_project_rule(request.project_rule, "allow", match_kind=request.match_kind)

    def _match_rules(self, target: MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            matched: PermissionRule | None = None
            rules = self._storage.rules_for_scope(scope)  # type: ignore[arg-type]
            for rule in rules:
                if rule_matches(rule.match, target, rule.match_kind):
                    matched = rule
            if matched is not None:
                return matched
        return None

    def _match_denied_rule(self, target: MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            for rule in reversed(self._storage.rules_for_scope(scope)):
                if rule.result == "deny" and rule_matches(rule.match, target, rule.match_kind):
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
