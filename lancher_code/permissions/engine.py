from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from lancher_code.permissions.models import PermissionDecision, PermissionRequest, PermissionResolution, PermissionRule
from lancher_code.contracts.control import PermissionPolicy
from lancher_code.contracts.tools import ToolCall, ToolDefinition, tool_available_in_phase
from lancher_code.tools.context import ToolContext
from lancher_code.filesystem.access import PathWriteDeniedError, ensure_writable_path, is_session_workspace_path
from lancher_code.permissions.models import PermissionCheck
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.permissions.rules import MatchTarget
from lancher_code.permissions.rules import rule_context_matches
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
            return self._evaluate_command(call, tool, context, target, metadata)
        if tool.name == "process_write":
            # 进程输入并不能确定解释器及它当前 cwd，不保存跨输入授权。
            from lancher_code.permissions.shell import analyze_command
            from lancher_code.permissions.rules import current_command_shell
            analysis = analyze_command(str(call.arguments.get("text", "")), shell=current_command_shell(),
                                       cwd=context.cwd, project_root=context.project_root or context.cwd)
            if analysis.hard_denials:
                return PermissionCheck(decision="deny", reason_code="permission_blacklist_denied",
                                       reason_message="；".join(analysis.hard_denials), metadata=metadata)
        if tool.name == "delete_file":
            from lancher_code.filesystem.deletion import plan_delete
            try:
                plan = plan_delete(call.arguments, context)
            except (ValueError, OSError) as exc:
                return PermissionCheck(decision="deny", reason_code=getattr(exc, "reason_code", "delete_target_invalid"),
                                       reason_message=str(exc), metadata=metadata)
            metadata["delete_signature"] = plan.signature
            denied = self._match_denied_rule(target)
            if denied is not None:
                return PermissionCheck(decision="deny", reason_code="permission_rule_deny",
                                       reason_message=f"命中 {denied.scope} 级拒绝规则: {denied.match}", metadata=metadata)
            if plan.is_directory or plan.recursive:
                request = build_permission_request(call, tool, context, target)
                return PermissionCheck(decision="ask", request=request, metadata=metadata)
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
            if tool.name == "process_write" or context.permission_policy != "bypass":
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
        command_context = ({"command_cwd": request.metadata.get("command_cwd"),
                            "command_shell": request.metadata.get("command_shell")}
                           if request.tool_name == "run_command" else {})
        if resolution.outcome == "allow_session" and request.session_rule:
            self._storage.add_session_rule(request.session_rule, "allow", match_kind=request.match_kind, **command_context)
        elif resolution.outcome == "allow_project" and request.project_rule:
            self._storage.add_project_rule(request.project_rule, "allow", match_kind=request.match_kind, **command_context)

    def _evaluate_command(self, call, tool, context, target, metadata) -> PermissionCheck:
        from hashlib import sha256
        import json
        from lancher_code.permissions.shell import analyze_command
        from lancher_code.filesystem.access import ensure_path_in_root
        from lancher_code.filesystem.deletion import plan_delete, resolve_delete_target

        analysis = analyze_command(target.exact_value or "", shell=target.command_shell,
                                   cwd=Path(target.command_cwd), project_root=context.project_root or context.cwd)
        metadata.update({"cwd": target.command_cwd, "command_cwd": target.command_cwd, "command_shell": target.command_shell,
                         "analysis_unknown": list(analysis.unknown_reasons),
                         "subcommands": [item.raw for item in analysis.commands],
                         "path_accesses": [{"path": str(item.path), "operation": item.operation,
                                            "recursive": item.recursive} for item in analysis.path_accesses]})
        if analysis.hard_denials:
            return PermissionCheck(decision="deny", reason_code="permission_blacklist_denied",
                                   reason_message="；".join(analysis.hard_denials), metadata=metadata)
        manifests = []
        try:
            for access in analysis.path_accesses:
                if access.operation == "execute":
                    # 安装在项目外的解释器和程序仍可启动；路径展示并要求本次
                    # 审批，不能把程序位置当成项目文件读取或写入目标。
                    continue
                ensure_path_in_root(access.path, context.project_root or context.cwd)
                if access.operation == "delete":
                    resolve_delete_target(str(access.path), context)
                    if access.path.exists() or access.path.is_symlink():
                        plan = plan_delete({"path": str(access.path), "recursive": access.recursive}, context)
                        manifests.append({"path": str(plan.target), "signature": plan.signature,
                                          "paths": [str(entry.path) for entry in plan.entries],
                                          "count": len(plan.entries), "bytes": plan.total_bytes})
                elif access.operation == "write":
                    ensure_writable_path(access.path, context)
                    if any(part.casefold() == ".git" for part in access.path.relative_to(context.project_root or context.cwd).parts):
                        raise PathWriteDeniedError("git_metadata_protected", "不能通过 Shell 修改 .git 元数据。")
        except (ValueError, OSError) as exc:
            return PermissionCheck(decision="deny", reason_code=getattr(exc, "reason_code", "path_outside_project"),
                                   reason_message=str(exc), metadata=metadata)
        metadata["delete_manifests"] = manifests
        signature_data = {key: metadata[key] for key in ("command_cwd", "command_shell", "analysis_unknown",
                                                       "subcommands", "path_accesses", "delete_manifests")}
        signature_data["command"] = target.exact_value
        metadata["execution_signature"] = sha256(json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

        # 拒绝规则对各个 AST 子命令生效；完整精确审批不会覆盖新出现的拒绝。
        subtargets = [replace(target, value=item.raw, exact_value=item.raw, command_cwd=str(item.cwd), command_name=item.name)
                      for item in analysis.commands]
        for candidate in (target, *subtargets):
            denied = self._match_rules(candidate)
            if denied is not None and denied.result == "deny":
                return PermissionCheck(decision="deny", reason_code="permission_rule_deny",
                                       reason_message=f"命中 {denied.scope} 级拒绝规则: {denied.match}", metadata=metadata)
        once_only = bool(analysis.unknown_reasons or any(item.recursive for item in analysis.deletes)
                         or len(analysis.deletes) > 1)
        if not once_only:
            exact = self._match_rules(target)
            # 整段 glob 不提供授权；每个子命令必须独立命中允许规则。
            if exact is not None and exact.result == "allow" and exact.match_kind == "exact":
                return PermissionCheck(decision="allow", metadata=metadata)
            if subtargets and all((matched := self._match_rules(item)) is not None and matched.result == "allow"
                                  for item in subtargets):
                return PermissionCheck(decision="allow", metadata=metadata)
            if context.permission_policy == "bypass":
                return PermissionCheck(decision="allow", metadata=metadata)
        request = build_permission_request(call, tool, context, target)
        request.metadata.update(metadata)
        request.metadata["allow_once_only"] = once_only
        if once_only:
            request.session_rule = request.project_rule = None
            request.prompt = "命令包含无法完整分析的操作或批量删除，请确认完整原文及目标；仅允许本次。"
        summary = [f"Shell：{analysis.shell}", f"子命令数：{len(analysis.commands)}"]
        summary.extend(f"{item.operation}：{item.path}" for item in analysis.path_accesses)
        summary.extend(f"无法确定：{reason}" for reason in analysis.unknown_reasons)
        for manifest in manifests:
            summary.append(f"删除清单：{manifest['count']} 项，{manifest['bytes']} 字节")
            summary.extend(manifest["paths"])
        request.details = "\n".join(summary) + "\n\n" + request.details
        request.metadata["analysis_summary"] = "\n".join(summary[:12])
        return PermissionCheck(decision="ask", request=request, metadata=metadata)

    def _match_rules(self, target: MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            matched: PermissionRule | None = None
            rules = self._storage.rules_for_scope(scope)  # type: ignore[arg-type]
            for rule in rules:
                if rule_context_matches(rule, target) and rule_matches(rule.match, target, rule.match_kind):
                    matched = rule
            if matched is not None:
                return matched
        return None

    def _match_denied_rule(self, target: MatchTarget) -> PermissionRule | None:
        for scope in ("session", "project", "user"):
            for rule in reversed(self._storage.rules_for_scope(scope)):
                if rule.result == "deny" and rule_context_matches(rule, target) and rule_matches(rule.match, target, rule.match_kind):
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
