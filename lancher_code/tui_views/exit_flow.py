from __future__ import annotations

import math
from collections.abc import Callable
from time import monotonic
from typing import Literal


ExitAction = Literal["cancel_work", "wait_for_stop", "arm_exit", "exit", "ignore"]
ExitState = Literal["idle", "stopping", "armed", "closing"]


class ExitFlow:
    """决定停止与退出的意图，界面负责提示和实际的异步收尾。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        confirmation_seconds: float = 3.0,
    ) -> None:
        if not math.isfinite(confirmation_seconds) or confirmation_seconds <= 0:
            raise ValueError("退出确认时限必须是有限的正数。")
        self._clock = clock
        self._confirmation_seconds = confirmation_seconds
        self._confirmation_deadline: float | None = None
        self._stop_requested = False
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def state(self) -> ExitState:
        if self._closing:
            return "closing"
        if self._stop_requested:
            return "stopping"
        return "armed" if self.is_armed else "idle"

    @property
    def is_armed(self) -> bool:
        return self.confirmation_deadline is not None

    @property
    def confirmation_deadline(self) -> float | None:
        self._expire_confirmation()
        return self._confirmation_deadline

    @property
    def confirmation_remaining(self) -> float:
        now = self._clock()
        self._expire_confirmation(now)
        if self._confirmation_deadline is None:
            return 0.0
        return self._confirmation_deadline - now

    def request_interrupt(self, *, busy: bool, stopping: bool = False) -> ExitAction:
        """工作中先停止；空闲时在确认窗口内再次按下才退出。"""
        if self._closing:
            return "ignore"
        if stopping or (busy and self._stop_requested):
            self._confirmation_deadline = None
            self._stop_requested = True
            return "wait_for_stop"
        if busy:
            self._confirmation_deadline = None
            self._stop_requested = True
            return "cancel_work"

        # 界面收尾事件可能还在队列中；真实工作已结束时允许重新确认退出。
        self._stop_requested = False
        now = self._clock()
        self._expire_confirmation(now)
        if self._confirmation_deadline is not None:
            return self.request_exit()
        self._confirmation_deadline = now + self._confirmation_seconds
        return "arm_exit"

    def request_exit(self) -> ExitAction:
        """显式退出命令已经表达退出意图，不再要求一次键盘确认。"""
        if self._closing:
            return "ignore"
        self._confirmation_deadline = None
        self._closing = True
        return "exit"

    def interact(self) -> None:
        """继续编辑或操作表示用户留下；不能撤回已经开始的退出。"""
        self._confirmation_deadline = None

    def work_started(self) -> None:
        if self._closing:
            return
        self.interact()
        self._stop_requested = False

    def work_finished(self) -> None:
        self._stop_requested = False

    def _expire_confirmation(self, now: float | None = None) -> None:
        if self._confirmation_deadline is None:
            return
        if now is None:
            now = self._clock()
        if now >= self._confirmation_deadline:
            self._confirmation_deadline = None
