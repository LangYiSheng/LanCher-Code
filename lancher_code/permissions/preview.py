from __future__ import annotations

import json
from uuid import uuid4
from lancher_code.permissions.models import PermissionRequest
from lancher_code.contracts.tools import ToolCall, ToolDefinition
from lancher_code.tools.context import ToolContext
from lancher_code.filesystem.access import ensure_path_in_root, relative_display_path, resolve_path_in_root
from lancher_code.permissions.rules import MatchTarget


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


def build_denied_metadata(
    call: ToolCall,
    tool: ToolDefinition,
    context: ToolContext,
    target: MatchTarget,
) -> dict[str, object]:
    metadata: dict[str, object] = {

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


def build_permission_request(
    call: ToolCall,
    tool: ToolDefinition,
    context: ToolContext,
    target: MatchTarget,
) -> PermissionRequest:
    request_id = f"perm-{uuid4().hex[:8]}"
    metadata: dict[str, object] = {

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
            tool_label=target.tool_label, kind="command",
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
