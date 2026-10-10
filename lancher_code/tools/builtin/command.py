from __future__ import annotations

from pathlib import Path

from lancher_code.execution.contracts import ProcessSpec, ResourceClaim
from lancher_code.tools.context import ToolContext
from lancher_code.contracts.tools import ToolDefinition, ToolExecutionResult
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.tools.core.base import build_tool_error, build_tool_success
from lancher_code.filesystem.access import resolve_path_in_root


def _integer(arguments: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 至 {maximum} 之间。")
    return value


class RunCommandTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_command",
            description=("启动受当前 Session 管理的 shell 命令。短命令返回退出结果；超过 yield_ms 仍运行则返回 process_id。"
                         "yield_ms 只限制本次等待，max_runtime_ms 才限制真实运行时间。"
                         "开发服务器等跨轮次任务必须明确 lifetime=session；turn 进程在本轮停止或结束时收尾。"
                         "普通命令用 pipe，需要终端交互时用 pty。文件读写和搜索优先使用对应文件工具。"),
            input_schema={"type": "object", "properties": {
                "description": {"type": "string", "minLength": 1, "pattern": r"\S", "description": "一句话说明命令用途。"},
                "command": {"type": "string", "minLength": 1, "pattern": r"\S", "description": "Windows 使用 PowerShell，POSIX 使用 /bin/sh。"},
                "cwd": {"type": "string", "minLength": 1, "pattern": r"\S", "description": "可选，项目内的现存工作目录。"},
                "transport": {"type": "string", "enum": ["pipe", "pty"], "default": "pipe"},
                "lifetime": {"type": "string", "enum": ["turn", "session"], "default": "turn"},
                "yield_ms": {"type": "integer", "minimum": 0, "maximum": 60000, "default": 1000},
                "max_runtime_ms": {"type": ["integer", "null"], "minimum": 1},
            }, "required": ["description", "command"], "additionalProperties": False},
            category="command", is_system_tool=True,
            allowed_phases=("execute",))

    def resource_claims(self, arguments: dict, context: ToolContext) -> tuple[ResourceClaim, ...]:
        cwd = self._cwd(arguments, context)
        if context.execution_runtime is None:
            return (ResourceClaim("project", str(context.project_root)),)
        return context.execution_runtime.command_claims(str(arguments.get("command", "")), cwd)

    def _cwd(self, arguments: dict, context: ToolContext) -> Path:
        raw = arguments.get("cwd")
        if raw is not None and (not isinstance(raw, str) or not raw.strip()):
            raise ValueError("cwd 必须是非空路径字符串。")
        return resolve_path_in_root(context.cwd, raw or ".", context.project_root or context.cwd)

    async def execute(self, arguments: dict, context: ToolContext) -> ToolExecutionResult:
        name = self.definition.name
        try:
            if context.work_phase != "execute":
                raise ValueError("执行命令需要 execute 阶段。")
            for key in ("command", "description"):
                if not isinstance(arguments.get(key), str) or not arguments[key].strip():
                    raise ValueError(f"{key} 必须是非空字符串。")
            lifetime = arguments.get("lifetime", "turn")
            transport = arguments.get("transport", "pipe")
            if lifetime not in {"turn", "session"} or transport not in {"pipe", "pty"}:
                raise ValueError("lifetime 或 transport 无效。")
            yield_ms = _integer(arguments, "yield_ms", 1000, 0, 60000)
            runtime_ms = arguments.get("max_runtime_ms")
            if runtime_ms is not None and (isinstance(runtime_ms, bool) or not isinstance(runtime_ms, int) or runtime_ms <= 0):
                raise ValueError("max_runtime_ms 必须是正整数或 null。")
            runtime = context.execution_runtime
            if runtime is None or context.session_id is None or context.invocation_id is None:
                raise ValueError("进程执行需要已注册的 Session 和工具调用身份。")
            command = arguments["command"].strip()
            spec = ProcessSpec(command, arguments["description"].strip(), self._cwd(arguments, context),
                               transport=transport, lifetime=lifetime, yield_ms=yield_ms,
                               max_runtime_ms=runtime_ms, readiness=runtime.command_readiness(command))
            info = await runtime.processes.start(spec, session_id=context.session_id,
                turn_id=context.turn_id, invocation_id=context.invocation_id,
                resource_lease=context.resource_lease, cancellation_token=context.cancellation_token)
            output = runtime.processes.read(info.process_id, context.session_id)
            metadata = info.to_dict() | output.to_dict()
            content = (f"描述: {info.description}\n进程: {info.process_id}\n状态: {info.status}\n"
                       f"退出码: {info.exit_code}\n{output.text or '(暂时没有输出)'}")
            if info.status in {"failed", "cancelled", "interrupted"} or info.exit_code not in {None, 0}:
                return build_tool_error(summary=f"命令结束：{info.exit_reason or info.exit_code}",
                    error_code=info.exit_reason if info.exit_reason not in {None, "completed"} else "non_zero_exit",
                    error_message=f"进程状态 {info.status}，退出码 {info.exit_code}。",
                    metadata=metadata, content=content, tool_name=name)
            return build_tool_success(summary="命令运行中" if info.status == "running" else "命令执行完成",
                                      content=content, metadata=metadata, tool_name=name)
        except SessionRepositoryError:
            raise
        except (ValueError, TypeError) as exc:
            return build_tool_error(summary="执行命令失败", error_code="invalid_arguments",
                                    error_message=str(exc), tool_name=name)
        except (OSError, RuntimeError) as exc:
            return build_tool_error(summary="执行命令失败", error_code="process_error",
                                    error_message=str(exc), tool_name=name)
