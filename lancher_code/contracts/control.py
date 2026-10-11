from __future__ import annotations

from typing import Literal
import asyncio


WorkPhase = Literal["discuss", "plan", "execute"]


ExecutionAccess = Literal["project", "full"]
AccessMode = Literal["read_only", "project", "full"]


def effective_access_mode(work_phase: WorkPhase, execution_access: ExecutionAccess) -> AccessMode:
    """讨论和计划强制只读，执行权限单独保存，返回执行时恢复。"""
    validate_runtime_axes(work_phase, execution_access)
    return "read_only" if work_phase in {"discuss", "plan"} else execution_access


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


def validate_runtime_axes(work_phase: WorkPhase, execution_access: ExecutionAccess) -> tuple[WorkPhase, ExecutionAccess]:
    """校验工作阶段和返回执行阶段时使用的访问范围。"""
    if work_phase not in {"discuss", "plan", "execute"}:
        raise ValueError("工作阶段无效。")
    if execution_access not in {"project", "full"}:
        raise ValueError("执行访问范围必须为 project 或 full。旧权限策略不再支持。")
    return work_phase, execution_access
