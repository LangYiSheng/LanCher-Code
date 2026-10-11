from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Literal

from lancher_code.contracts.control import effective_access_mode
from lancher_code.contracts.tools import ToolCall, ToolDefinition, tool_available_in_phase
from lancher_code.filesystem.access import PathWriteDeniedError, ensure_writable_path, resolve_access_path
from lancher_code.permissions.models import DirectoryGrant, PermissionCheck, PermissionRequest, PermissionResolution, PermissionRule
from lancher_code.permissions.preview import build_denied_metadata, build_permission_request
from lancher_code.permissions.rules import MatchTarget, build_match_target, current_command_shell, rule_context_matches, rule_matches
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.tools.context import ToolContext


@dataclass(frozen=True, slots=True)
class _PathUse:
    path: Path
    access: Literal["read", "write"]
    recursive: bool = False
    operation: str = "read"
    glob_prefix: Path | None = None


def _whole_directory_pattern(component: str) -> bool:
    """这些模式与 * 一样覆盖所有非空名称，不包含按扩展名等定向筛选。"""
    return "*" in component and set(component) <= {"*", "?"} and component.count("?") <= 1


class PermissionEngine:
    """访问范围决定审批；明确极危险操作和用户拒绝规则优先拦截。"""

    def __init__(self, storage: PermissionStorage | None = None) -> None:
        self._storage = storage or PermissionStorage()

    @property
    def storage(self) -> PermissionStorage:
        return self._storage

    def evaluate(self, *, call: ToolCall, tool: ToolDefinition, context: ToolContext) -> PermissionCheck:
        mode = effective_access_mode(context.work_phase, context.execution_access)
        basic = {"cwd": str(context.cwd), "work_phase": context.work_phase,
                 "execution_access": context.execution_access, "access_mode": mode, "tool_name": tool.name}
        if not tool_available_in_phase(tool, context.work_phase):
            return PermissionCheck("deny", "phase_disallowed", f"当前 {context.work_phase} 阶段不允许执行该工具。", metadata=basic)
        external = tool.permission is not None and tool.permission.source == "external"
        if mode == "read_only" and tool.category != "read" and tool.name not in {"run_command", "write_plan_file", "process_stop"}:
            return PermissionCheck("deny", "phase_disallowed", "讨论和计划阶段只允许读取；计划文件需使用专用工具。", metadata=basic)
        if external and mode == "read_only" and tool.category != "read":
            return PermissionCheck("deny", "phase_disallowed", "只读阶段不能调用可能产生远程副作用的工具。", metadata=basic)
        try:
            target = build_match_target(call, tool, context)
            metadata = {**build_denied_metadata(call, tool, context, target), **basic}
            if tool.name == "run_command" and not external:
                return self._evaluate_command(call, tool, context, target, metadata)
            paths = [] if external else self._native_paths(call, tool, context, metadata)
            if tool.name == "delete_file" and not external:
                from lancher_code.filesystem.deletion import plan_delete
                plan = plan_delete(call.arguments, context)
                metadata["delete_signature"] = plan.signature
            if tool.name == "process_write" and not external:
                from lancher_code.permissions.shell import analyze_command
                analysis = analyze_command(str(call.arguments.get("text", "")), shell=current_command_shell(),
                                           cwd=paths[0].path if paths else context.cwd,
                                           project_root=context.project_root or context.cwd)
                metadata["analysis_unknown"] = list(analysis.unknown_reasons)
                metadata["subcommands"] = [item.raw for item in analysis.commands]
                metadata["process_cwd_is_startup_snapshot"] = True
                metadata["command_shell"] = analysis.shell
                if analysis.hard_denials:
                    return PermissionCheck("deny", "permission_blacklist_denied", "；".join(analysis.hard_denials), metadata=metadata)
                for item in analysis.commands:
                    candidate = MatchTarget("run_command", "RunCommand", item.raw, item.raw,
                                            str(item.cwd), analysis.shell, item.name)
                    denied = self._match_denied_rule(candidate)
                    if denied is not None:
                        return self._rule_denied(denied, metadata)
                paths.extend(self._analyzed_paths(analysis, context, metadata, include_cwd=False))
        except (ValueError, OSError) as exc:
            return PermissionCheck("deny", getattr(exc, "reason_code", "invalid_access_target"), str(exc), metadata=basic)
        denied = self._match_denied_rule(target)
        if denied is not None:
            return self._rule_denied(denied, metadata)
        self._sign_access(call, context, paths, metadata)
        if external:
            if tool.category == "read" or mode == "full":
                return PermissionCheck("allow", metadata=metadata)
            request = build_permission_request(call, tool, context, target)
            request.metadata.update(metadata)
            return PermissionCheck("ask", request=request, metadata=metadata)
        return self._access_decision(call, tool, context, target, paths, metadata)

    def apply_resolution(self, request: PermissionRequest, resolution: PermissionResolution) -> None:
        if resolution.request_id != request.request_id:
            raise ValueError("审批结果不属于当前请求。")
        if resolution.outcome not in {"allow_once", "allow_session", "deny", "superseded"}:
            raise ValueError("审批结果无效；旧项目长期授权不再支持。")
        if resolution.outcome == "allow_session":
            if not request.directory_grants:
                raise ValueError("当前请求仅支持本次批准，不支持本会话目录授权。")
            self._storage.add_directory_grants(request.directory_grants)

    def _evaluate_command(self, call, tool, context, target, metadata) -> PermissionCheck:
        from lancher_code.permissions.shell import analyze_command
        analysis = analyze_command(target.exact_value or "", shell=target.command_shell,
                                   cwd=Path(target.command_cwd), project_root=context.project_root or context.cwd)
        metadata.update(cwd=target.command_cwd, command_cwd=target.command_cwd, command_shell=target.command_shell,
                        analysis_unknown=list(analysis.unknown_reasons), subcommands=[item.raw for item in analysis.commands])
        if analysis.hard_denials:
            return PermissionCheck("deny", "permission_blacklist_denied", "；".join(analysis.hard_denials), metadata=metadata)
        if metadata["access_mode"] == "read_only" and not (analysis.complete and analysis.read_only):
            return PermissionCheck("deny", "phase_disallowed", "只读阶段仅允许能完整识别为读取的命令。", metadata=metadata)
        try:
            paths = self._analyzed_paths(analysis, context, metadata)
        except (ValueError, OSError) as exc:
            return PermissionCheck("deny", getattr(exc, "reason_code", "invalid_access_target"), str(exc), metadata=metadata)
        candidates = [target, *(replace(target, value=item.raw, exact_value=item.raw,
                        command_cwd=str(item.cwd), command_name=item.name) for item in analysis.commands)]
        for candidate in candidates:
            denied = self._match_denied_rule(candidate)
            if denied is not None:
                return self._rule_denied(denied, metadata)
        self._sign_access(call, context, paths, metadata)
        check = self._access_decision(call, tool, context, target, paths, metadata)
        if check.request is not None:
            summary = [f"Shell：{analysis.shell}", f"子命令数：{len(analysis.commands)}"]
            summary.extend(f"{item.operation}：{item.path}" for item in paths)
            summary.extend(f"分析不确定：{reason}" for reason in analysis.unknown_reasons)
            for manifest in metadata.get("delete_manifests", []):
                summary.append(f"删除清单：{manifest['count']} 项，{manifest['bytes']} 字节")
                summary.extend(manifest["paths"])
            check.request.details = "\n".join(summary) + "\n\n" + check.request.details
            check.request.metadata["analysis_summary"] = "\n".join(summary[:12])
        return check

    @staticmethod
    def _analyzed_paths(analysis, context: ToolContext, metadata: dict, *, include_cwd: bool = True) -> list[_PathUse]:
        from lancher_code.filesystem.deletion import plan_delete, protected_delete_reason, resolve_delete_target
        # 启动外部工作目录的任意命令需要读写授权；完整只读命令只需要读取。
        paths = ([_PathUse(Path(metadata.get("command_cwd", analysis.commands[0].cwd if analysis.commands else context.cwd)),
                           "read" if analysis.complete and analysis.read_only else "write", True, "cwd")]
                 if include_cwd else [])
        manifests = []
        for access in analysis.path_accesses:
            if access.operation == "execute":
                # 安装在项目外的解释器位置不等于它读写的用户文件目录。
                continue
            path = access.path
            access_kind = "write" if access.operation in {"write", "delete"} else "read"
            recursive = access.recursive
            if access.operation == "delete":
                if any(_whole_directory_pattern(part) for part in path.parts):
                    wildcard_index = next(index for index, part in enumerate(path.parts) if _whole_directory_pattern(part))
                    prefix = Path(*path.parts[:wildcard_index])
                    reason = protected_delete_reason(prefix, context, recursive=access.recursive)
                    if reason:
                        raise PathWriteDeniedError(reason, "通配符删除会覆盖受保护的整体目录。")
                path = resolve_delete_target(str(path), context)
                if access.recursive and (path.is_symlink() or path.is_junction()):
                    # Shell 的递归链接删除语义取决于实现，按可能触及真实目标审阅。
                    linked_target = path.resolve()
                    reason = protected_delete_reason(linked_target, context, recursive=True)
                    if reason:
                        raise PathWriteDeniedError(reason, "Shell 递归删除链接可能触及受保护的真实目录。")
                    paths.append(_PathUse(linked_target, "write", True, "delete_link_target"))
                    if linked_target.exists():
                        linked_plan = plan_delete({"path": str(linked_target), "recursive": True}, context)
                        manifests.append({"path": str(linked_plan.target), "signature": linked_plan.signature,
                                          "paths": [str(entry.path) for entry in linked_plan.entries],
                                          "count": len(linked_plan.entries), "bytes": linked_plan.total_bytes})
                if path.exists() or path.is_symlink():
                    # Shell 支持 rmdir 空目录等非递归删除；采集完整版本清单不改变实际命令。
                    recursive = access.recursive or (path.is_dir() and not path.is_symlink() and not path.is_junction())
                    plan = plan_delete({"path": str(path), "recursive": recursive}, context)
                    manifests.append({"path": str(plan.target), "signature": plan.signature,
                                      "paths": [str(entry.path) for entry in plan.entries],
                                      "count": len(plan.entries), "bytes": plan.total_bytes})
            elif access.operation == "write":
                path = ensure_writable_path(path, context)
            else:
                path = path.resolve()
                if access.operation == "cwd":
                    access_kind = "read" if analysis.complete and analysis.read_only else "write"
                    recursive = True
            glob_prefix = None
            if access.operation in {"read", "write", "delete"}:
                raw_parts = Path(access.raw).parts
                wildcard_index = next((index for index, part in enumerate(raw_parts)
                                       if any(character in part for character in "*?[")), None)
                if wildcard_index is not None:
                    # 只解释原始 Shell 参数中的模式，不能把工作目录的字面量括号当模式。
                    suffix = raw_parts[wildcard_index:]
                    if tuple(path.parts[-len(suffix):]) == suffix:
                        glob_prefix = Path(*path.parts[:-len(suffix)]).resolve()
                    else:
                        raw_prefix = Path(*raw_parts[:wildcard_index])
                        command_cwd = analysis.commands[access.command_index].cwd
                        glob_prefix = (raw_prefix if raw_prefix.is_absolute() else command_cwd / raw_prefix).resolve()
            paths.append(_PathUse(path, access_kind, recursive, access.operation, glob_prefix))
        metadata["delete_manifests"] = manifests
        return paths

    @staticmethod
    def _native_paths(call: ToolCall, tool: ToolDefinition, context: ToolContext, metadata: dict) -> list[_PathUse]:
        name = tool.name
        if name == "write_plan_file":
            if context.plan_file_path is None:
                raise PathWriteDeniedError("missing_plan_file_path", "当前上下文没有有效的会话计划文件路径。")
            path = ensure_writable_path(context.plan_file_path, context, allow_plan_file=True)
            return [_PathUse(path, "write", operation="plan")]
        if name == "delete_file":
            from lancher_code.filesystem.deletion import resolve_delete_target
            path = resolve_delete_target(str(call.arguments.get("path", "")), context)
            return [_PathUse(path, "write", bool(call.arguments.get("recursive", False)), "delete")]
        if name in {"write_file", "edit_file", "read_file", "glob", "grep"}:
            raw_path = call.arguments.get("path")
            if name in {"glob", "grep"} and raw_path is None:
                path = (context.project_root or context.cwd).resolve()
            elif isinstance(raw_path, str) and raw_path.strip():
                path = resolve_access_path(context.cwd, raw_path, context)
            else:
                raise ValueError("path 必须是非空路径字符串。")
            write = name in {"write_file", "edit_file"}
            if write:
                path = ensure_writable_path(path, context)
            return [_PathUse(path, "write" if write else "read", name in {"glob", "grep"} and not path.is_file(),
                             "write" if write else "read")]
        if name in {"process_write", "process_background"} and context.execution_runtime is not None and context.session_id:
            info = context.execution_runtime.processes.get(str(call.arguments.get("process_id", "")), context.session_id)
            metadata["process_command"] = getattr(info, "command", None)
            metadata["process_cwd"] = info.cwd
            return [_PathUse(Path(info.cwd).resolve(), "write", True, "process_cwd")]
        return []

    @staticmethod
    def _sign_access(call: ToolCall, context: ToolContext, paths: list[_PathUse], metadata: dict) -> None:
        accesses = [{"path": str(item.path), "access": item.access, "recursive": item.recursive,
                     "operation": item.operation} for item in paths]
        metadata["path_accesses"] = accesses
        frozen = {"call": call.arguments, "tool_name": call.tool_name,
                  "access_mode": effective_access_mode(context.work_phase, context.execution_access),
                  "paths": accesses, "command_cwd": metadata.get("command_cwd"), "command_shell": metadata.get("command_shell"),
                  "subcommands": metadata.get("subcommands"), "analysis_unknown": metadata.get("analysis_unknown"),
                  "process_command": metadata.get("process_command"), "process_cwd": metadata.get("process_cwd"),
                  "delete_signature": metadata.get("delete_signature"), "delete_manifests": metadata.get("delete_manifests")}
        metadata["execution_signature"] = sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    def _access_decision(self, call, tool, context, target, paths, metadata) -> PermissionCheck:
        root = (context.project_root or context.cwd).resolve()
        required = [item for item in paths if not item.path.is_relative_to(root)]
        if metadata["access_mode"] == "full" or not required:
            return PermissionCheck("allow", metadata=metadata)
        missing = [item for item in required if not self._covered(item)]
        if not missing:
            return PermissionCheck("allow", "session_directory_allowed", "已批准当前会话的目录访问范围。", metadata=metadata)
        grants_by_path: dict[str, DirectoryGrant] = {}
        for item in missing:
            # 文件授权界面明确授予它所在目录；目录和 cwd 使用目标目录本身。
            if item.glob_prefix is not None:
                # 通配符表达式不是目录，授权范围只能取第一个 glob 组件之前的真实目录。
                directory = item.glob_prefix
            else:
                directory = item.path if item.path.is_dir() and not item.path.is_symlink() else item.path.parent
            grant = DirectoryGrant(str(directory), item.access, True)
            existing = grants_by_path.get(grant.path)
            if existing is None or grant.access == "write":
                grants_by_path[grant.path] = grant
        grants = list(grants_by_path.values())
        request = build_permission_request(call, tool, context, target)
        request.directory_grants = grants
        request.metadata.update(metadata)
        request.metadata["external_accesses"] = [{"path": str(item.path), "access": item.access,
                                                   "recursive": item.recursive} for item in missing]
        request.metadata["allow_once_only"] = False
        request.details += "\n\n本会话目录授权范围：\n" + "\n".join(
            f"{grant.path}：{'读写' if grant.access == 'write' else '只读'}，包含子目录" for grant in grants)
        return PermissionCheck("ask", request=request, metadata=metadata)

    def _covered(self, use: _PathUse) -> bool:
        for grant in self._storage.session_grants:
            root = Path(grant.path)
            if use.access == "write" and grant.access != "write":
                continue
            if grant.recursive and use.path.is_relative_to(root):
                return True
            if not grant.recursive and not use.recursive and (use.path == root or use.path.parent == root):
                return True
        return False

    def _match_denied_rule(self, target: MatchTarget) -> PermissionRule | None:
        # 每层只有拒绝规则；任何匹配拒绝都会生效，目录授权不能覆盖拒绝。
        for scope in ("session", "project", "user"):
            for rule in reversed(self._storage.rules_for_scope(scope)):
                if rule_context_matches(rule, target) and rule_matches(rule.match, target, rule.match_kind):
                    return rule
        return None

    @staticmethod
    def _rule_denied(rule: PermissionRule, metadata: dict) -> PermissionCheck:
        return PermissionCheck("deny", "permission_rule_deny", f"命中 {rule.scope} 级拒绝规则: {rule.match}",
                               metadata={**metadata, "rule_match": rule.match, "rule_scope": rule.scope})
