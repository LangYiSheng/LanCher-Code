from __future__ import annotations

import platform
from datetime import date
from pathlib import Path

from lancher_code.models import RuntimeMode


def build_system_prompt(*, cwd: Path, current_date: date, mode: RuntimeMode, plan_file_path: Path) -> str:
    sections = [
        _role_section(),
        _environment_section(cwd=cwd, current_date=current_date),
        _mode_section(mode=mode, plan_file_path=plan_file_path),
    ]
    return "\n\n".join(sections)


def _role_section() -> str:
    return (
        "你是 LanCher Code，一个终端里的 AI 编程助手。\n"
        "优先基于真实代码和工具结果行动，不要猜测。\n"
        "需要信息时继续调用工具，直到信息足够再给出结论。\n"
        "工具失败时要根据错误调整，而不是直接放弃。"
    )


def _environment_section(*, cwd: Path, current_date: date) -> str:
    return (
        "环境信息：\n"
        f"- 当前系统：{_runtime_label()}\n"
        f"- 当前工作目录：{cwd.resolve()}\n"
        f"- 当前日期：{current_date.isoformat()}"
    )


def _mode_section(*, mode: RuntimeMode, plan_file_path: Path) -> str:
    if mode == "plan":
        return (
            "模式指令（Plan Mode）：\n"
            "1. 你不能执行任何仓库修改操作，不能编辑普通文件、不能提交代码、不能修改配置。\n"
            "2. 你只能使用只读工具探索代码；唯一允许写入的文件是计划文件。\n"
            f"3. 计划文件固定为：{plan_file_path.resolve()}\n"
            "4. 你的工作流程是：先读取代码与上下文，再整理实现方案，写入计划文件，等待用户确认。\n"
            "5. 不要通过命令重定向、启动脚本或其他旁路方式绕过这些限制。"
        )

    return (
        "模式指令（Normal Mode）：\n"
        "1. 可以使用当前暴露的全部工具完成任务。\n"
        "2. 修改文件前先读取相关文件；如果工具提示文件已变化，先重新读取再继续。\n"
        "3. 在一次回复里可以多次调用工具，直到任务完成或命中安全停止条件。"
    )


def _runtime_label() -> str:
    system = platform.system()
    if system == "Windows":
        return "Windows PowerShell"
    if system == "Linux":
        return "Linux shell"
    if system == "Darwin":
        return "macOS shell"
    return f"{system or 'Unknown'} shell"
