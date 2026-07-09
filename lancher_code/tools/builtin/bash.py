from __future__ import annotations

import asyncio
import re

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.permission_engine import validate_plan_command
from lancher_code.tools.core.base import build_tool_error, build_tool_success

MAX_OUTPUT_CHARS = 12000
POWERSHELL = "C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"

BASH_DESCRIPTION = (
    "执行 shell 命令，是唯一能直接与操作系统交互的工具。"
    "适合查看目录、运行测试、读取 git 状态、调用现有 CLI。"
    "如果 read_file、glob、grep、edit_file、write_file 能完成任务，就不要优先使用它。"
    "调用时必须同时提供 description 和 command："
    "description 用一句简短的话说明这条命令是在做什么，"
    "command 则是实际要在当前工作目录执行的命令。"
)
 

class BashTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            description=BASH_DESCRIPTION,
            params_model={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "简单一句话，描述这条命令是在做什么。",
                    },
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令。",
                    },
                },
                "required": ["description", "command"],
                "additionalProperties": False,
            },
            category="command",
            is_concurrency_safe=False,
            is_system_tool=True,
            allowed_modes=("default", "plan", "acceptEdits", "bypass"),
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        description = arguments.get("description")
        command = arguments.get("command")

        if not isinstance(description, str) or not description.strip():
            return build_tool_error(
                summary="执行命令失败",
                error_code="invalid_arguments",
                error_message="description 必须是非空字符串，用一句短语说明命令用途。",
                tool_name=self.definition.name,
            )
        if not isinstance(command, str) or not command.strip():
            return build_tool_error(
                summary="执行命令失败",
                error_code="invalid_arguments",
                error_message="command 必须是非空字符串。",
                tool_name=self.definition.name,
            )

        description = description.strip()
        command = command.strip()
        if context.mode == "plan":
            plan_rejection = validate_plan_command(command)
            if plan_rejection is not None:
                return build_tool_error(
                    summary="Plan 模式禁止该命令",
                    error_code="plan_mode_command_rejected",
                    error_message=plan_rejection,
                    metadata={"description": description, "command": command, "mode": context.mode},
                    tool_name=self.definition.name,
                )

        try:
            process = await asyncio.create_subprocess_exec(
                POWERSHELL,
                "-NoProfile",
                "-Command",
                command,
                cwd=str(context.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return build_tool_error(
                summary="执行命令失败",
                error_code="spawn_error",
                error_message=str(exc),
                metadata={"description": description, "command": command},
                tool_name=self.definition.name,
            )

        communicate_task = asyncio.create_task(process.communicate())
        cancel_wait_task: asyncio.Task[None] | None = None
        try:
            if context.cancellation_token is not None:
                cancel_wait_task = asyncio.create_task(context.cancellation_token.wait())
                done, _pending = await asyncio.wait(
                    {communicate_task, cancel_wait_task},
                    timeout=context.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_wait_task in done and context.cancellation_token.is_cancelled:
                    process.kill()
                    await communicate_task
                    raise asyncio.CancelledError
                if communicate_task not in done:
                    process.kill()
                    await communicate_task
                    return build_tool_error(
                        summary="命令执行超时",
                        error_code="command_timeout",
                        error_message=f"命令在 {context.timeout_seconds} 秒内没有完成，已被终止。",
                        metadata={"description": description, "command": command},
                        tool_name=self.definition.name,
                    )
                stdout, stderr = communicate_task.result()
            else:
                try:
                    stdout, stderr = await asyncio.wait_for(communicate_task, timeout=context.timeout_seconds)
                except asyncio.TimeoutError:
                    process.kill()
                    await communicate_task
                    return build_tool_error(
                        summary="命令执行超时",
                        error_code="command_timeout",
                        error_message=f"命令在 {context.timeout_seconds} 秒内没有完成，已被终止。",
                        metadata={"description": description, "command": command},
                        tool_name=self.definition.name,
                    )
        except asyncio.CancelledError:
            process.kill()
            await asyncio.gather(communicate_task, return_exceptions=True)
            raise
        finally:
            if cancel_wait_task is not None:
                cancel_wait_task.cancel()
                await asyncio.gather(cancel_wait_task, return_exceptions=True)

        stdout_text, stdout_truncated = _truncate_output(stdout.decode("utf-8", errors="replace"))
        stderr_text, stderr_truncated = _truncate_output(stderr.decode("utf-8", errors="replace"))
        exit_code = process.returncode or 0
        payload = {
            "description": description,
            "command": command,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "truncated": stdout_truncated or stderr_truncated,
        }
        content = _format_command_content(description, command, exit_code, stdout_text, stderr_text)
        is_error = _is_error_exit(command, exit_code)

        if is_error:
            return build_tool_error(
                summary=f"命令退出码非零: {exit_code}",
                error_code="non_zero_exit",
                error_message=f"命令退出码非零: {exit_code}",
                metadata=payload,
                content=content,
                tool_name=self.definition.name,
            )

        return build_tool_success(
            summary="命令执行完成",
            content=content,
            metadata=payload,
            tool_name=self.definition.name,
        )


def _truncate_output(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS] + "\n... [输出已截断]", True


def _format_command_content(description: str, command: str, exit_code: int, stdout: str, stderr: str) -> str:
    return (
        f"描述: {description}\n"
        f"命令: {command}\n"
        f"退出码: {exit_code}\n"
        f"stdout:\n{stdout or '(空)'}\n"
        f"stderr:\n{stderr or '(空)'}"
    )


def _is_error_exit(command: str, exit_code: int) -> bool:
    if exit_code == 0:
        return False
    if _is_special_non_zero_command(command):
        return exit_code > 2
    return True


def _is_special_non_zero_command(command: str) -> bool:
    lowered = command.strip().lower()
    tokens = re.findall(r"[a-zA-Z0-9_.-]+", lowered)
    if not tokens:
        return False
    if tokens[0] in {"grep", "find", "diff", "rg", "fc", "select-string"}:
        return True
    return len(tokens) >= 2 and tokens[0] == "git" and tokens[1] == "diff"


RunCommandTool = BashTool
