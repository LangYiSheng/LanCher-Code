from __future__ import annotations

import json

from lancher_code.execution.contracts import ResourceClaim
from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.sessions.repository import SessionRepositoryError
from lancher_code.tools.builtin.command import _integer
from lancher_code.tools.core.base import build_tool_error, build_tool_success


_DESCRIPTIONS = {
    "process_list": "查看当前 Session 的所有托管进程及退出记录。",
    "process_read": "按字符游标读取进程输出；next_cursor 用于下次读取，读取不消费其他观察者的输出。",
    "process_wait": "等待当前 Session 的进程退出；timeout_ms 只限制本次等待，超时不会停止进程。",
    "process_write": "向当前 Session 的运行进程写入 stdin；换行需在 text 中明确提供。",
    "process_stop": "停止当前 Session 的指定进程及托管子进程，保留日志；重复停止安全。",
    "process_background": "把本轮运行进程明确转交给 Session，使它能跨轮次、跨界面切换继续运行。",
}


class ProcessTool:
    def __init__(self, name: str) -> None:
        if name not in _DESCRIPTIONS:
            raise ValueError("未知进程工具。")
        self.name = name

    @property
    def definition(self) -> ToolDefinition:
        properties: dict[str, object] = {}
        required = []
        if self.name != "process_list":
            properties["process_id"] = {"type": "string", "pattern": r"^[0-9a-f]{32}$", "description": "当前 Session 的进程 UUID。"}
            required = ["process_id"]
        if self.name == "process_read":
            properties.update({"cursor": {"type": "integer", "minimum": 0, "default": 0},
                               "max_chars": {"type": "integer", "minimum": 1, "maximum": 16000, "default": 16000}})
        elif self.name == "process_wait":
            properties["timeout_ms"] = {"type": "integer", "minimum": 0, "maximum": 60000, "default": 1000}
        elif self.name == "process_write":
            properties["text"] = {"type": "string", "maxLength": 65536}
            required.append("text")
        read_only = self.name in {"process_list", "process_read", "process_wait"}
        return ToolDefinition(name=self.name, description=_DESCRIPTIONS[self.name],
            params_model={"type": "object", "properties": properties,
                          "required": required, "additionalProperties": False},
            category="read" if read_only else "command", is_system_tool=True,
            allowed_modes=("default", "plan", "acceptEdits", "bypass") if read_only
                          else ("default", "acceptEdits", "bypass"))

    def resource_claims(self, arguments: dict, context: ToolContext) -> tuple[ResourceClaim, ...]:
        if self.name in {"process_write", "process_background"}:
            return (ResourceClaim("process", f"{arguments.get('process_id', '')}:stdin"),)
        # 停止/读取不能排在被目标进程长期持有的项目写锁之后。
        return ()

    async def execute(self, arguments: dict, context: ToolContext) -> ToolExecutionResult:
        try:
            if context.execution_runtime is None or context.session_id is None:
                raise ValueError("进程工具需要活动 Session。")
            supervisor = context.execution_runtime.processes
            session_id = context.session_id
            process_id = arguments.get("process_id")
            if self.name != "process_list" and (not isinstance(process_id, str) or not process_id):
                raise ValueError("process_id 必须是进程 UUID。")
            if self.name == "process_list":
                metadata = {"processes": [info.to_dict() for info in supervisor.list(session_id)]}
            elif self.name == "process_read":
                cursor = _integer(arguments, "cursor", 0, 0, 2 ** 63 - 1)
                budget = _integer(arguments, "max_chars", 16000, 1, 16000)
                metadata = supervisor.read(process_id, session_id, cursor=cursor, max_chars=budget).to_dict()
                metadata["process_id"] = process_id
            elif self.name == "process_wait":
                timeout = _integer(arguments, "timeout_ms", 1000, 0, 60000)
                metadata = (await supervisor.wait(process_id, session_id, timeout_ms=timeout)).to_dict()
            elif self.name == "process_write":
                await supervisor.write(process_id, session_id, arguments.get("text"))
                metadata = supervisor.get(process_id, session_id).to_dict()
            elif self.name == "process_stop":
                metadata = (await supervisor.stop(process_id, session_id)).to_dict()
            else:
                metadata = (await supervisor.background(process_id, session_id)).to_dict()
            content = str(metadata["text"]) if self.name == "process_read" else json.dumps(metadata, ensure_ascii=False)
            return build_tool_success(summary=_DESCRIPTIONS[self.name].split("；")[0], content=content,
                                      metadata=metadata, tool_name=self.name)
        except SessionRepositoryError:
            raise
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            return build_tool_error(summary="进程操作失败", error_code="process_error",
                                    error_message=str(exc), tool_name=self.name)


def create_process_tools() -> list[ProcessTool]:
    return [ProcessTool(name) for name in _DESCRIPTIONS]
