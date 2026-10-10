from __future__ import annotations

import asyncio
import os
import weakref
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable

from lancher_code.contracts.control import CancellationToken


from lancher_code.execution.contracts import ResourceClaim, ResourceLifetime, ResourceOwner


def path_claim(path: Path, *, write: bool = False, recursive: bool = False,
               lifetime: ResourceLifetime = "process") -> ResourceClaim:
    return ResourceClaim("path", canonical_path(path), "exclusive" if write else "shared", recursive, lifetime)


def project_claim(root: Path, *, lifetime: ResourceLifetime = "process") -> ResourceClaim:
    return ResourceClaim("project", canonical_path(root), "exclusive", True, lifetime)


def normalize_claim(claim: ResourceClaim) -> ResourceClaim:
    if claim.kind in {"path", "project"}:
        return ResourceClaim(claim.kind, canonical_path(Path(claim.key)), claim.mode,
                             claim.recursive or claim.kind == "project", claim.lifetime)
    return claim


def canonical_path(path: Path) -> str:
    """已有链接、相对路径和 Windows 大小写都映射到同一资源。"""
    return os.path.normcase(str(path.resolve())).replace("\\", "/").rstrip("/") or "/"


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def claims_conflict(left: ResourceClaim, right: ResourceClaim) -> bool:
    if left.mode == right.mode == "shared":
        return False
    local = {"path", "project"}
    if left.kind in local and right.kind in local:
        return (
            left.key == right.key
            or (left.recursive and _within(right.key, left.key))
            or (right.recursive and _within(left.key, right.key))
        )
    # 未知 Shell/MCP 的项目独占声明覆盖本地文件和未知外部副作用，
    # 但不会挡住读取输出或停止进程这些管理入口。
    if {left.kind, right.kind} == {"project", "external"}:
        return True
    return left.kind == right.kind and left.key == right.key


@dataclass(slots=True)
class _Waiter:
    claims: tuple[ResourceClaim, ...]
    future: asyncio.Future[ResourceLease]
    counted: bool = True
    owner: ResourceOwner | None = None
    updates: asyncio.Queue[dict] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    last_snapshot: dict | None = None


class ResourceLease:
    """一组资源的原子租约；转交后台进程后由进程退出回收。"""

    def __init__(self, scheduler: ResourceScheduler, claims: tuple[ResourceClaim, ...], *,
                 counted: bool = True, owner: ResourceOwner | None = None) -> None:
        self._scheduler = scheduler
        self.claims = claims
        self.counted = counted
        self.owner = owner
        self.transferred = False
        self.released = False

    def transfer(self) -> None:
        if self.released:
            raise RuntimeError("已释放的资源租约不能转交。")
        if not self.transferred:
            self.transferred = True
            self._scheduler._pump()

    def bind_process(self, process_id: str) -> None:
        """进程身份得到确认后更新等待者快照，不保存命令正文。"""
        if self.owner is not None:
            self.owner = replace(self.owner, process_id=process_id)
            self._scheduler._pump()

    async def finish_invocation(self) -> None:
        """调用结束释放排序锁；真实资源仍由已接管的进程持有。"""
        if self.released:
            return
        if not self.transferred:
            await self.release()
            return
        retained = tuple(claim for claim in self.claims if claim.lifetime == "process")
        if not retained:
            await self.release()
        elif retained != self.claims:
            self.claims = retained
            self._scheduler._pump()

    async def release(self) -> None:
        if not self.released:
            self.released = True
            self._scheduler._active.remove(self)
            self._scheduler._pump()

    async def __aenter__(self) -> ResourceLease:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.finish_invocation()


class ResourceScheduler:
    """冲突请求遵守 FIFO，无冲突请求可以前进；一次授予全部资源。"""

    def __init__(self, *, max_concurrency: int = 8) -> None:
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("工具并发上限必须是正整数。")
        self.max_concurrency = max_concurrency
        self._waiters: list[_Waiter] = []
        self._active: list[ResourceLease] = []

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    async def reserve(
        self, claims: tuple[ResourceClaim, ...] | list[ResourceClaim],
        *, cancellation_token: CancellationToken | None = None, counted: bool = True,
        owner: ResourceOwner | None = None, on_wait: Callable[[dict], Awaitable[None]] | None = None,
    ) -> ResourceLease:
        if cancellation_token is not None and cancellation_token.is_cancelled:
            raise asyncio.CancelledError
        if owner is not None and not isinstance(owner, ResourceOwner):
            raise TypeError("资源占用者必须是 ResourceOwner。")
        raw = tuple(claims)
        if any(not isinstance(claim, ResourceClaim) for claim in raw):
            raise TypeError("调度器只接收 ResourceClaim。")
        unique = tuple(dict.fromkeys(normalize_claim(claim) for claim in raw))
        if any(not isinstance(claim, ResourceClaim) for claim in unique):
            raise TypeError("调度器只接收 ResourceClaim。")
        if any(claim.kind not in {"path", "project", "process", "external"} or claim.mode not in {"shared", "exclusive"}
               or claim.lifetime not in {"invocation", "process"} or not claim.key for claim in unique):
            raise ValueError("资源声明无效。")
        waiter = _Waiter(unique, asyncio.get_running_loop().create_future(), counted, owner)
        self._waiters.append(waiter)
        self._pump()
        cancellation_wait = None
        notification_wait = None
        try:
            if cancellation_token is not None:
                cancellation_wait = asyncio.create_task(cancellation_token.wait())
                # 即时授予也给取消信号一个调度边界；同时取消不能把租约交给已停止的调用。
                if waiter.future.done():
                    await asyncio.wait({waiter.future, cancellation_wait}, return_when=asyncio.FIRST_COMPLETED)
            while True:
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError
                if waiter.future.done():
                    lease = await asyncio.shield(waiter.future)
                    # 租约还没交给调用者；所有可能被取消的清理都必须在回收 except 内。
                    await self._finish_wait_tasks(notification_wait, cancellation_wait)
                    if cancellation_token is not None and cancellation_token.is_cancelled:
                        raise asyncio.CancelledError
                    notification_wait = cancellation_wait = None
                    return lease  # 此后 finally 无 await，交接不会再留下取消窗口。
                waits = {waiter.future}
                if cancellation_wait is not None:
                    waits.add(cancellation_wait)
                if on_wait is not None:
                    notification_wait = asyncio.create_task(waiter.updates.get())
                    waits.add(notification_wait)
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                if cancellation_token is not None and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError
                if notification_wait is not None:
                    if notification_wait.done() and not waiter.future.done():
                        # 同步 pump 只记录最新快照，通知在此处执行；失败会走同一回收入口。
                        notification = asyncio.create_task(on_wait(notification_wait.result()))
                        try:
                            if cancellation_wait is not None:
                                await asyncio.wait({notification, cancellation_wait}, return_when=asyncio.FIRST_COMPLETED)
                                if cancellation_token.is_cancelled:
                                    raise asyncio.CancelledError
                            await notification
                        finally:
                            if not notification.done():
                                notification.cancel()
                            await asyncio.gather(notification, return_exceptions=True)
                    else:
                        notification_wait.cancel()
                    await asyncio.gather(notification_wait, return_exceptions=True)
                    notification_wait = None
        except BaseException:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
                waiter.future.cancel()
            elif waiter.future.done() and not waiter.future.cancelled():
                await waiter.future.result().release()
            self._pump()
            raise
        finally:
            if notification_wait is not None or cancellation_wait is not None:
                # 异常路径先撤销排队/租约，再收拢辅助任务；二次停止不能留下后台等待者。
                await self._finish_wait_tasks(notification_wait, cancellation_wait)

    @staticmethod
    async def _finish_wait_tasks(*tasks: asyncio.Task | None) -> None:
        remaining = [task for task in tasks if task is not None]
        if not remaining:
            return
        for task in remaining:
            task.cancel()
        settling = asyncio.gather(*remaining, return_exceptions=True)
        cancellation = None
        while not settling.done():
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError as exc:
                # 辅助 Task 必须真的退出；随后仍把取消交给外层统一回收，绝不吞掉停止。
                cancellation = exc
        settling.result()
        if cancellation is not None:
            raise cancellation

    def _pump(self) -> None:
        """所有状态变更在同一事件循环内同步完成，授予中途不会让出控制权。"""
        earlier: list[_Waiter] = []
        for waiter in list(self._waiters):
            if waiter.future.cancelled():
                self._waiters.remove(waiter)
                continue
            blocking = [lease for lease in self._active if self._conflict(waiter.claims, lease.claims)]
            if blocking:
                self._queue_snapshot(waiter, "resource_conflict", blocking, conflicting_only=True)
                earlier.append(waiter)
                continue
            capacity = [lease for lease in self._active if lease.counted and not lease.transferred]
            if waiter.counted and len(capacity) >= self.max_concurrency:
                self._queue_snapshot(waiter, "capacity", capacity)
                earlier.append(waiter)
                continue
            predecessors = [previous for previous in earlier if self._conflict(waiter.claims, previous.claims)]
            if predecessors:
                self._queue_snapshot(waiter, "fifo", predecessors, conflicting_only=True)
                earlier.append(waiter)
                continue
            lease = ResourceLease(self, waiter.claims, counted=waiter.counted, owner=waiter.owner)
            self._active.append(lease)
            self._waiters.remove(waiter)
            waiter.future.set_result(lease)

    def _queue_snapshot(self, waiter: _Waiter, reason: str, blockers, *, conflicting_only=False) -> None:
        snapshot = {"reason": reason, "blockers": [self._blocker(item, waiter.claims if conflicting_only else None)
                    for item in blockers], "limit": self.max_concurrency}
        if snapshot != waiter.last_snapshot:
            waiter.last_snapshot = snapshot
            if waiter.updates.full():
                waiter.updates.get_nowait()
            waiter.updates.put_nowait(snapshot)

    @staticmethod
    def _blocker(item, requested=None) -> dict:
        identity = asdict(item.owner) if item.owner is not None else {
            "session_id": None, "invocation_id": None, "tool_name": None, "process_id": None}
        return {**identity, "resources": [asdict(claim) for claim in item.claims
            if requested is None or any(claims_conflict(claim, other) for other in requested)]}

    def describe_blockers(self, claims: tuple[ResourceClaim, ...] | list[ResourceClaim]) -> dict:
        """即时活动资源快照；实际排队的额度与 FIFO 原因由 on_wait 提供。"""
        normalized = tuple(normalize_claim(claim) for claim in claims)
        blocking = [lease for lease in self._active if self._conflict(normalized, lease.claims)]
        return {"reason": "resource_conflict" if blocking else None,
                "blockers": [self._blocker(item, normalized) for item in blocking], "limit": self.max_concurrency}

    @staticmethod
    def _conflict(left: tuple[ResourceClaim, ...], right: tuple[ResourceClaim, ...]) -> bool:
        return any(claims_conflict(one, other) for one in left for other in right)


_PROJECT_SCHEDULERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, ResourceScheduler]] = weakref.WeakKeyDictionary()


def get_project_scheduler(root: Path, *, max_concurrency: int = 8) -> ResourceScheduler:
    """同一应用事件循环中的多个 Session 共用项目资源锁。"""
    projects = _PROJECT_SCHEDULERS.setdefault(asyncio.get_running_loop(), {})
    key = canonical_path(root)
    scheduler = projects.get(key)
    if scheduler is None:
        scheduler = ResourceScheduler(max_concurrency=max_concurrency)
        projects[key] = scheduler
    elif scheduler.max_concurrency != max_concurrency:
        raise ValueError("同一项目的工具并发上限必须一致。")
    return scheduler
