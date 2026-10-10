from __future__ import annotations

import asyncio
import os
import weakref
from dataclasses import dataclass
from pathlib import Path

from lancher_code.models import CancellationToken


from lancher_code.execution.contracts import ResourceClaim


def path_claim(path: Path, *, write: bool = False, recursive: bool = False) -> ResourceClaim:
    return ResourceClaim("path", canonical_path(path), "exclusive" if write else "shared", recursive)


def project_claim(root: Path) -> ResourceClaim:
    return ResourceClaim("project", canonical_path(root), "exclusive", True)


def normalize_claim(claim: ResourceClaim) -> ResourceClaim:
    if claim.kind in {"path", "project"}:
        return ResourceClaim(claim.kind, canonical_path(Path(claim.key)), claim.mode,
                             claim.recursive or claim.kind == "project")
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


class ResourceLease:
    """一组资源的原子租约；转交后台进程后由进程退出回收。"""

    def __init__(self, scheduler: ResourceScheduler, claims: tuple[ResourceClaim, ...], *, counted: bool = True) -> None:
        self._scheduler = scheduler
        self.claims = claims
        self.counted = counted
        self.transferred = False
        self.released = False

    def transfer(self) -> None:
        if self.released:
            raise RuntimeError("已释放的资源租约不能转交。")
        if not self.transferred:
            self.transferred = True
            self._scheduler._pump()

    async def release(self) -> None:
        if not self.released:
            self.released = True
            self._scheduler._active.remove(self)
            self._scheduler._pump()

    async def __aenter__(self) -> ResourceLease:
        return self

    async def __aexit__(self, *_: object) -> None:
        if not self.transferred:
            await self.release()


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
    ) -> ResourceLease:
        if cancellation_token is not None and cancellation_token.is_cancelled:
            raise asyncio.CancelledError
        raw = tuple(claims)
        if any(not isinstance(claim, ResourceClaim) for claim in raw):
            raise TypeError("调度器只接收 ResourceClaim。")
        unique = tuple(dict.fromkeys(normalize_claim(claim) for claim in raw))
        if any(not isinstance(claim, ResourceClaim) for claim in unique):
            raise TypeError("调度器只接收 ResourceClaim。")
        if any(claim.kind not in {"path", "project", "process", "external"} or claim.mode not in {"shared", "exclusive"} or not claim.key for claim in unique):
            raise ValueError("资源声明无效。")
        waiter = _Waiter(unique, asyncio.get_running_loop().create_future(), counted)
        self._waiters.append(waiter)
        self._pump()
        cancellation_wait = None
        try:
            if cancellation_token is not None:
                cancellation_wait = asyncio.create_task(cancellation_token.wait())
                await asyncio.wait((waiter.future, cancellation_wait), return_when=asyncio.FIRST_COMPLETED)
                if cancellation_token.is_cancelled:
                    raise asyncio.CancelledError
            return await asyncio.shield(waiter.future)
        except BaseException:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
                waiter.future.cancel()
            elif waiter.future.done() and not waiter.future.cancelled():
                await waiter.future.result().release()
            self._pump()
            raise
        finally:
            if cancellation_wait is not None:
                cancellation_wait.cancel()
                await asyncio.gather(cancellation_wait, return_exceptions=True)

    def _pump(self) -> None:
        """所有状态变更在同一事件循环内同步完成，授予中途不会让出控制权。"""
        earlier: list[_Waiter] = []
        for waiter in list(self._waiters):
            if waiter.future.cancelled():
                self._waiters.remove(waiter)
                continue
            if waiter.counted and sum(lease.counted and not lease.transferred for lease in self._active) >= self.max_concurrency:
                earlier.append(waiter)
                continue
            if any(self._conflict(waiter.claims, lease.claims) for lease in self._active):
                earlier.append(waiter)
                continue
            if any(self._conflict(waiter.claims, previous.claims) for previous in earlier):
                earlier.append(waiter)
                continue
            lease = ResourceLease(self, waiter.claims, counted=waiter.counted)
            self._active.append(lease)
            self._waiters.remove(waiter)
            waiter.future.set_result(lease)

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
