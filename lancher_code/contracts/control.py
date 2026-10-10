from __future__ import annotations

from typing import Literal
import asyncio


WorkPhase = Literal["discuss", "plan", "execute"]


PermissionPolicy = Literal["default", "acceptEdits", "bypass"]


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


def validate_runtime_axes(work_phase: WorkPhase, permission_policy: PermissionPolicy) -> tuple[WorkPhase, PermissionPolicy]:
    """校验独立的工作阶段与权限策略。"""
    if work_phase not in {"discuss", "plan", "execute"}:
        raise ValueError("工作阶段无效。")
    if permission_policy not in {"default", "acceptEdits", "bypass"}:
        raise ValueError("权限策略无效。")
    return work_phase, permission_policy
