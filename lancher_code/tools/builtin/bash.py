from __future__ import annotations

import asyncio
import re

from lancher_code.models import ToolContext, ToolDefinition, ToolExecutionResult
from lancher_code.tools.core.base import build_tool_error, build_tool_success

MAX_OUTPUT_CHARS = 12000
POWERSHELL = "C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"

BASH_DESCRIPTION = (
    "执行 shell 命令，是唯一直接与操作系统交互的工具。"
    "适合查看目录、运行测试、读取 git 状态、启动构建、调用现有 CLI。"
    "不要在能用 read_file、glob、grep、edit_file、write_file 完成时滥用它，因为那些工具更结构化、更安全。"
    "参数只有 command，必须是一条可直接在当前工作目录执行的命令。"
    "返回给模型的 content 会包含退出码、stdout、stderr；metadata 会保留原始输出和截断信息供 UI 使用。"
    "对于 grep、diff、find、rg、git diff 这类命令，退出码 1 或 2 可能代表“无结果”或“有差异”，不一定视为错误。"
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
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令。请写成一条完整命令。",
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            category="command",
            is_concurrency_safe=False,
            is_system_tool=True,
        )

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return build_tool_error(
                summary="执行命令失败",
                error_code="invalid_arguments",
                error_message="command 必须是非空字符串。",
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
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=context.timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return build_tool_error(
                    summary="命令执行超时",
                    error_code="command_timeout",
                    error_message=f"命令在 {context.timeout_seconds} 秒内没有完成，已被终止。",
                    metadata={"command": command},
                    tool_name=self.definition.name,
                )
        except Exception as exc:
            return build_tool_error(
                summary="执行命令失败",
                error_code="spawn_error",
                error_message=str(exc),
                metadata={"command": command},
                tool_name=self.definition.name,
            )

        stdout_text, stdout_truncated = _truncate_output(stdout.decode("utf-8", errors="replace"))
        stderr_text, stderr_truncated = _truncate_output(stderr.decode("utf-8", errors="replace"))
        exit_code = process.returncode or 0
        payload = {
            "command": command,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "truncated": stdout_truncated or stderr_truncated,
        }
        content = _format_command_content(command, exit_code, stdout_text, stderr_text)
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


def _format_command_content(command: str, exit_code: int, stdout: str, stderr: str) -> str:
    return (
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
    if tokens[0] in {"grep", "find", "diff", "rg", "fc"}:
        return True
    return len(tokens) >= 2 and tokens[0] == "git" and tokens[1] == "diff"


RunCommandTool = BashTool
