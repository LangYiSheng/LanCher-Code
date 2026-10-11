from __future__ import annotations

import json
from uuid import uuid4

from lancher_code.contracts.tools import ToolCall, ToolDefinition
from lancher_code.filesystem.access import relative_display_path, resolve_access_path
from lancher_code.permissions.models import PermissionRequest
from lancher_code.permissions.rules import MatchTarget
from lancher_code.tools.context import ToolContext


def _request_file_paths(tool_name: str, arguments: dict[str, object], context: ToolContext) -> list[str]:
    project_root = context.project_root or context.cwd
    if tool_name == "delete_file":
        from lancher_code.filesystem.deletion import plan_delete
        paths = [entry.path for entry in plan_delete(arguments, context).entries]
        # 删除链接展示链接本身，不跟随叶子链接显示另一个目标。
        return [str(path.relative_to(project_root.resolve())) if path.is_relative_to(project_root.resolve()) else str(path)
                for path in paths]
    if tool_name == "write_plan_file":
        return [relative_display_path(context.plan_file_path, project_root)] if context.plan_file_path is not None else []
    raw_path = arguments.get("path")
    if tool_name in {"glob", "grep"} and raw_path is None:
        return [str(project_root.resolve())]
    if not isinstance(raw_path, str) or not raw_path.strip():
        return []
    return [str(resolve_access_path(context.cwd, raw_path, context))]


def _build_preview_lines(tool_name: str, arguments: dict[str, object], context: ToolContext) -> list[dict[str, str]]:
    if tool_name == "edit_file":
        old_text, new_text = arguments.get("old_text"), arguments.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return []
        return ([{"text": f"- {line}", "tone": "error"} for line in old_text.splitlines()[:10] or [old_text]]
                + [{"text": f"+ {line}", "tone": "success"} for line in new_text.splitlines()[:10] or [new_text]])
    if tool_name in {"write_file", "write_plan_file"}:
        content = arguments.get("content")
        if not isinstance(content, str):
            return []
        return [{"text": f"+ {line}", "tone": "success"} for line in content.splitlines()[:10] or ([""] if content == "" else [])]
    return []


def build_denied_metadata(call: ToolCall, tool: ToolDefinition, context: ToolContext, target: MatchTarget) -> dict[str, object]:
    metadata: dict[str, object] = {
        "cwd": str(context.cwd), "work_phase": context.work_phase,
        "execution_access": context.execution_access, "tool_name": tool.name, "tool_label": target.tool_label,
    }
    if tool.name == "run_command":
        metadata.update(command=str(call.arguments.get("command", "")).strip(),
                        description=str(call.arguments.get("description", "")).strip())
    elif tool.name in {"read_file", "write_file", "edit_file", "delete_file", "write_plan_file", "glob", "grep"}:
        try:
            metadata["paths"] = _request_file_paths(tool.name, call.arguments, context)
        except (ValueError, OSError):
            metadata["paths"] = []
        preview_lines = _build_preview_lines(tool.name, call.arguments, context)
        if preview_lines:
            metadata["display_lines"] = preview_lines
    return metadata


def build_permission_request(call: ToolCall, tool: ToolDefinition, context: ToolContext, target: MatchTarget) -> PermissionRequest:
    metadata: dict[str, object] = {"cwd": str(context.cwd), "work_phase": context.work_phase,
                                   "execution_access": context.execution_access}
    common = dict(request_id=f"perm-{uuid4().hex[:8]}", call_id=call.call_id, tool_name=tool.name,
                  tool_label=target.tool_label, work_phase=context.work_phase, execution_access=context.execution_access)
    if tool.permission is not None and tool.permission.source == "external":
        return PermissionRequest(**common, kind="external_tool", title="是否允许调用 MCP 工具",
            prompt=f"{tool.permission.server_name}/{tool.permission.remote_tool_name} 可能产生远程副作用；仅批准本次。",
            details="参数: " + json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str),
            metadata={**metadata, "server": tool.permission.server_name or "",
                      "remote_tool": tool.permission.remote_tool_name or "", "allow_once_only": True})
    if tool.name == "run_command":
        command = str(call.arguments.get("command", "")).strip()
        description = str(call.arguments.get("description", "")).strip()
        lifetime = call.arguments.get("lifetime", "turn")
        transport, yield_ms = call.arguments.get("transport", "pipe"), call.arguments.get("yield_ms", 1000)
        maximum = call.arguments.get("max_runtime_ms")
        return PermissionRequest(**common, kind="command", title="是否允许访问项目外目录",
            prompt="命令会访问以下项目外路径，请确认访问范围。", command=command, description=description,
            details=(f"命令: {command}\n描述: {description or '(无描述)'}\n工作目录: {target.command_cwd}\n"
                     f"归属: {lifetime}\n终端: {transport}\n本次等待: {yield_ms} 毫秒\n"
                     f"运行上限: {maximum if maximum is not None else '未设置'}"),
            metadata={**metadata, "cwd": target.command_cwd, "command_cwd": target.command_cwd,
                      "command_shell": target.command_shell, "lifetime": lifetime, "transport": transport,
                      "yield_ms": yield_ms, "max_runtime_ms": maximum})
    if tool.name in {"process_write", "process_background"}:
        arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return PermissionRequest(**common, kind="command", title="是否允许操作项目外进程",
            prompt="进程启动时的目录在项目外，运行中的实际目录可能变化。", details=arguments,
            command=arguments, metadata=metadata)
    paths = _request_file_paths(tool.name, call.arguments, context)
    if tool.name == "delete_file":
        from lancher_code.filesystem.deletion import plan_delete
        plan = plan_delete(call.arguments, context)
        return PermissionRequest(**common, kind="file_edit", title="是否允许删除项目外目标",
            prompt="删除会移除目标，请核对完整清单及授权目录。", file_paths=paths,
            details=f"删除项数：{len(plan.entries)}\n普通文件字节数：{plan.total_bytes}\n" + "\n".join(paths),
            metadata={**metadata, "delete_signature": plan.signature, "delete_count": len(plan.entries),
                      "delete_bytes": plan.total_bytes})
    reading = tool.category == "read"
    return PermissionRequest(**common, kind="file_edit", title="是否允许读取项目外路径" if reading else "是否允许修改项目外路径",
        prompt="请确认本次访问及可选的本会话目录授权。", details="\n".join(paths), file_paths=paths,
        preview_lines=_build_preview_lines(tool.name, call.arguments, context), metadata=metadata)
