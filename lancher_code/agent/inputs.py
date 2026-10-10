"""待处理输入的编辑与选择；会话状态和持久化由调用方维护。"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from uuid import uuid4

from lancher_code.errors import ConfigError
from lancher_code.sessions.models import PendingInput


class PendingInputQueue:
    def __init__(self, *, read: Callable[[], list[PendingInput]],
                 write: Callable[[list[PendingInput], str | None], None],
                 active: Callable[[], tuple[str | None, bool]], on_steer: Callable[[], None]):
        self._read = read
        self._write = write
        self._active = active
        self._on_steer = on_steer

    @property
    def items(self) -> list[PendingInput]:
        return deepcopy(self._read())

    @property
    def paused(self) -> bool:
        return any(item.state == "paused" for item in self._read())

    @staticmethod
    def _validate(text: str, delivery: str) -> None:
        if not text.strip() or text.lstrip().startswith("/"):
            raise ConfigError("待处理消息不能为空，也不能是斜杠命令。")
        if delivery not in {"follow_up", "steer"}:
            raise ConfigError("未知的消息发送方式。")

    def enqueue(self, text: str, delivery: str = "follow_up") -> PendingInput:
        self._validate(text, delivery)
        task_id, can_steer = self._active()
        item = PendingInput(
            id=uuid4().hex, text=text.strip(), delivery=delivery,
            target_task_id=task_id if delivery == "steer" and can_steer else None,
            state=("pending" if can_steer else "paused") if delivery == "steer"
                  else ("paused" if self.paused else "pending"),
        )
        self._write([*self.items, item], item.id)
        if delivery == "steer" and can_steer:
            self._on_steer()
        return deepcopy(item)

    def _find(self, item_id: str) -> tuple[list[PendingInput], PendingInput]:
        items = self.items
        item = next((item for item in items if item.id == item_id), None)
        if item is None:
            raise ConfigError("这条消息已经生效或被移除，请刷新待处理列表。")
        return items, item

    def update(self, item_id: str, text: str) -> PendingInput:
        items, item = self._find(item_id)
        self._validate(text, item.delivery)
        item.text = text.strip()
        self._write(items, item_id)
        return deepcopy(item)

    def remove(self, item_id: str) -> None:
        items, _ = self._find(item_id)
        self._write([item for item in items if item.id != item_id], item_id)

    def convert(self, item_id: str, delivery: str) -> PendingInput:
        items, item = self._find(item_id)
        self._validate(item.text, delivery)
        task_id, can_steer = self._active()
        item.delivery = delivery
        item.target_task_id = task_id if delivery == "steer" and can_steer else None
        if delivery == "steer":
            item.state = "pending" if can_steer else "paused"
        self._write(items, item_id)
        if delivery == "steer" and can_steer and item.state == "pending":
            self._on_steer()
        return deepcopy(item)

    def pause(self) -> None:
        items = self.items
        if not items or all(item.state == "paused" for item in items):
            return
        for item in items:
            item.state = "paused"
        self._write(items, None)

    def resume(self) -> None:
        items = self.items
        if not items:
            return
        task_id, _ = self._active()
        for item in items:
            item.state = "pending"
            if task_id is None or item.target_task_id != task_id:
                item.delivery = "follow_up"
                item.target_task_id = None
        self._write(items, None)
        if self.has_steering():
            self._on_steer()

    def has_steering(self) -> bool:
        task_id, _ = self._active()
        return task_id is not None and any(
            item.delivery == "steer" and item.state == "pending" and item.target_task_id == task_id
            for item in self._read()
        )

    def take_steering(self) -> list[PendingInput]:
        task_id, _ = self._active()
        selected = [item for item in self.items if item.delivery == "steer"
                    and item.state == "pending" and item.target_task_id == task_id]
        selected_ids = {item.id for item in selected}
        self._write([item for item in self.items if item.id not in selected_ids], None)
        return selected

    def take_next(self) -> PendingInput | None:
        items = self.items
        if self.paused or not items or items[0].state != "pending" or items[0].delivery != "follow_up":
            return None
        item = items.pop(0)
        self._write(items, item.id)
        return item
