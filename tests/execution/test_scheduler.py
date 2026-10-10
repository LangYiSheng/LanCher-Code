from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.execution.contracts import ResourceClaim, ResourceOwner
from lancher_code.execution.scheduler import ResourceScheduler, get_project_scheduler, path_claim, project_claim
from lancher_code.models import CancellationToken


@pytest.mark.asyncio
async def test_independent_paths_parallel_but_same_path_fifo(tmp_path: Path) -> None:
    scheduler = ResourceScheduler(max_concurrency=4)
    first = await scheduler.reserve([path_claim(tmp_path / "a", write=True)])
    waiting_write = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "a", write=True)]))
    waiting_read = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "a")]))
    independent = await scheduler.reserve([path_claim(tmp_path / "b", write=True)])
    await asyncio.sleep(0)
    assert not waiting_write.done() and not waiting_read.done()
    await first.release()
    second = await asyncio.wait_for(waiting_write, 1)
    assert not waiting_read.done()
    await second.release()
    third = await asyncio.wait_for(waiting_read, 1)
    await third.release()
    await independent.release()
    assert scheduler.active_count == scheduler.waiting_count == 0


@pytest.mark.asyncio
async def test_directory_claim_conflicts_with_descendant_but_not_prefix_sibling(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    lease = await scheduler.reserve([path_claim(tmp_path / "out", write=True, recursive=True)])
    child = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "out" / "a")]))
    sibling = await scheduler.reserve([path_claim(tmp_path / "output")])
    await asyncio.sleep(0)
    assert not child.done()
    await lease.release()
    await (await child).release()
    await sibling.release()


@pytest.mark.asyncio
async def test_multi_resource_claims_are_granted_together_without_hold_and_wait(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    owner = await scheduler.reserve([path_claim(tmp_path / "b", write=True)])
    both = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "a", write=True), path_claim(tmp_path / "b", write=True)]))
    await asyncio.sleep(0)
    assert scheduler.active_count == 1
    # 排队的写者使后来读者等待，避免不断来的读者饿死写者。
    late = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "a")]))
    await asyncio.sleep(0)
    assert not both.done() and not late.done()
    await owner.release()
    combined = await both
    assert not late.done()
    await combined.release()
    await (await late).release()


@pytest.mark.asyncio
async def test_cancel_waiting_claim_removes_it_and_never_acquires(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    lease = await scheduler.reserve([project_claim(tmp_path)])
    token = CancellationToken()
    waiting = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "x")], cancellation_token=token))
    await asyncio.sleep(0)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert scheduler.waiting_count == 0 and scheduler.active_count == 1
    await lease.release()


@pytest.mark.asyncio
async def test_cancel_just_granted_claim_does_not_leak(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    token = CancellationToken()
    pending = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "x")], cancellation_token=token))
    await asyncio.sleep(0)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert scheduler.active_count == scheduler.waiting_count == 0


@pytest.mark.asyncio
async def test_transferred_process_keeps_resources_without_taking_tool_capacity(tmp_path: Path) -> None:
    scheduler = ResourceScheduler(max_concurrency=1)
    process = await scheduler.reserve([path_claim(tmp_path / "cache", write=True, recursive=True)])
    process.transfer()
    ordinary = await scheduler.reserve([path_claim(tmp_path / "src")])
    same = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "cache" / "a")]))
    await ordinary.release()
    await asyncio.sleep(0)
    assert not same.done()
    await process.release()
    await (await same).release()
    await process.release()  # 停止、退出回调都可安全调用回收。


@pytest.mark.asyncio
async def test_concurrency_limit_applies_even_without_resource_conflicts() -> None:
    scheduler = ResourceScheduler(max_concurrency=1)
    first = await scheduler.reserve([])
    second = asyncio.create_task(scheduler.reserve([]))
    await asyncio.sleep(0)
    assert not second.done()
    await first.release()
    await (await second).release()


@pytest.mark.asyncio
async def test_same_project_sessions_share_scheduler(tmp_path: Path) -> None:
    assert get_project_scheduler(tmp_path) is get_project_scheduler(tmp_path / ".")


@pytest.mark.asyncio
async def test_process_management_is_not_blocked_by_unknown_project_command(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    command = await scheduler.reserve([project_claim(tmp_path)])
    management = await scheduler.reserve([ResourceClaim("process", "uuid:stdin")])
    await management.release()
    await command.release()


@pytest.mark.asyncio
async def test_existing_link_alias_uses_the_same_path_resource(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许创建符号链接。")
    assert path_claim(link / "a") == path_claim(target / "a")


@pytest.mark.asyncio
async def test_management_lane_can_pass_full_normal_capacity() -> None:
    scheduler = ResourceScheduler(max_concurrency=1)
    owner = await scheduler.reserve([])
    regular = asyncio.create_task(scheduler.reserve([]))
    await asyncio.sleep(0)
    management = await asyncio.wait_for(scheduler.reserve([], counted=False), 1)
    assert not regular.done()
    await management.release()
    await owner.release()
    await (await regular).release()


@pytest.mark.asyncio
async def test_management_lane_still_obeys_resource_fifo() -> None:
    scheduler = ResourceScheduler(max_concurrency=1)
    claim = ResourceClaim("process", "uuid:stdin")
    owner = await scheduler.reserve([claim])
    writer = asyncio.create_task(scheduler.reserve([claim]))
    await asyncio.sleep(0)
    transfer = asyncio.create_task(scheduler.reserve([claim], counted=False))
    await asyncio.sleep(0)
    assert not writer.done() and not transfer.done()
    await owner.release()
    writing = await writer
    assert not transfer.done()
    await writing.release()
    await (await transfer).release()


@pytest.mark.asyncio
async def test_invocation_project_lock_stays_until_handle_is_returned(tmp_path: Path) -> None:
    scheduler = ResourceScheduler(max_concurrency=1)
    server = await scheduler.reserve([project_claim(tmp_path, lifetime="invocation")])
    server.transfer()
    client = asyncio.create_task(scheduler.reserve([project_claim(tmp_path, lifetime="invocation")]))
    await asyncio.sleep(0)
    assert not client.done()  # 创建、初次等待仍属于本次调用，不能提前丢锁。
    await server.finish_invocation()
    await (await asyncio.wait_for(client, 1)).finish_invocation()
    await server.release()
    assert scheduler.active_count == scheduler.waiting_count == 0


@pytest.mark.asyncio
async def test_finishing_invocation_preserves_explicit_process_resources(tmp_path: Path) -> None:
    scheduler = ResourceScheduler(max_concurrency=2)
    server = await scheduler.reserve([
        project_claim(tmp_path, lifetime="invocation"),
        path_claim(tmp_path / "cache", write=True, recursive=True),
    ])
    server.transfer()
    await server.finish_invocation()
    unrelated = await scheduler.reserve([path_claim(tmp_path / "src")])
    same = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "cache" / "a")]))
    await asyncio.sleep(0)
    assert not same.done()
    assert all(claim.lifetime == "process" for claim in server.claims)
    await server.finish_invocation()  # 重复完成不能释放进程的真实资源。
    assert not same.done()
    await server.release()
    await (await asyncio.wait_for(same, 1)).release()
    await unrelated.release()


@pytest.mark.asyncio
async def test_live_wait_snapshot_identifies_process_and_updates_remaining_blockers(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    a = await scheduler.reserve([path_claim(tmp_path / "a", write=True)],
        owner=ResourceOwner("session-a", "invocation-a", "run_command"))
    a.transfer()
    a.bind_process("process-a")
    b = await scheduler.reserve([path_claim(tmp_path / "b", write=True)],
        owner=ResourceOwner("session-b", "invocation-b", "run_command"))
    snapshots = asyncio.Queue()

    async def on_wait(snapshot):
        await snapshots.put(snapshot)

    waiter = asyncio.create_task(scheduler.reserve([project_claim(tmp_path)], on_wait=on_wait))
    try:
        first = await asyncio.wait_for(snapshots.get(), 1)
        assert first["reason"] == "resource_conflict"
        assert {item["invocation_id"] for item in first["blockers"]} == {"invocation-a", "invocation-b"}
        assert first["blockers"][0]["process_id"] == "process-a"
        assert first["blockers"][0]["resources"][0]["lifetime"] == "process"
        await a.release()
        second = await asyncio.wait_for(snapshots.get(), 1)
        assert [item["invocation_id"] for item in second["blockers"]] == ["invocation-b"]
        assert not waiter.done()
        await b.release()
        await (await asyncio.wait_for(waiter, 1)).release()
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await a.release()
        await b.release()


@pytest.mark.asyncio
async def test_failed_wait_callback_cleans_up_lease_granted_during_notification(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    owner = await scheduler.reserve([project_claim(tmp_path)])

    async def on_wait(snapshot):
        await owner.release()
        raise RuntimeError("通知失败")

    with pytest.raises(RuntimeError, match="通知失败"):
        await scheduler.reserve([project_claim(tmp_path)], on_wait=on_wait)
    assert scheduler.active_count == scheduler.waiting_count == 0


@pytest.mark.asyncio
async def test_token_cancels_blocked_wait_notification_without_leaking(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    owner = await scheduler.reserve([project_claim(tmp_path)])
    token = CancellationToken()
    entered = asyncio.Event()

    async def on_wait(snapshot):
        entered.set()
        await asyncio.Event().wait()

    waiter = asyncio.create_task(scheduler.reserve([project_claim(tmp_path)],
        cancellation_token=token, on_wait=on_wait))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, 1)
        assert scheduler.waiting_count == 0 and scheduler.active_count == 1
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await owner.release()


@pytest.mark.asyncio
async def test_capacity_wait_snapshot_names_only_slot_owners() -> None:
    scheduler = ResourceScheduler(max_concurrency=1)
    slot = await scheduler.reserve([], owner=ResourceOwner("session", "slot", "process_wait"))
    updates = asyncio.Queue()

    async def notify(snapshot):
        await updates.put(snapshot)

    waiting = asyncio.create_task(scheduler.reserve([], on_wait=notify))
    try:
        snapshot = await asyncio.wait_for(updates.get(), 1)
        assert snapshot["reason"] == "capacity"
        assert snapshot["blockers"] == [{"session_id": "session", "invocation_id": "slot",
            "tool_name": "process_wait", "process_id": None, "resources": []}]
        await slot.release()
        await (await asyncio.wait_for(waiting, 1)).release()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await slot.release()


@pytest.mark.asyncio
async def test_fifo_snapshot_names_conflicting_predecessor_not_unrelated_owner(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    owner = await scheduler.reserve([path_claim(tmp_path / "b", write=True)])
    first = asyncio.create_task(scheduler.reserve([
        path_claim(tmp_path / "a", write=True), path_claim(tmp_path / "b", write=True)],
        owner=ResourceOwner("session", "predecessor", "write_file")))
    await asyncio.sleep(0)
    updates = asyncio.Queue()

    async def notify(snapshot):
        await updates.put(snapshot)

    later = asyncio.create_task(scheduler.reserve([path_claim(tmp_path / "a")], on_wait=notify))
    try:
        snapshot = await asyncio.wait_for(updates.get(), 1)
        assert snapshot["reason"] == "fifo"
        assert [item["invocation_id"] for item in snapshot["blockers"]] == ["predecessor"]
        assert len(snapshot["blockers"][0]["resources"]) == 1
        await owner.release()
        await (await asyncio.wait_for(first, 1)).release()
        await (await asyncio.wait_for(later, 1)).release()
    finally:
        first.cancel()
        later.cancel()
        await asyncio.gather(first, later, return_exceptions=True)
        await owner.release()


@pytest.mark.asyncio
async def test_owner_validation_precedes_waiter_registration() -> None:
    scheduler = ResourceScheduler()
    with pytest.raises(TypeError, match="ResourceOwner"):
        await scheduler.reserve([], owner={"invocation_id": "not-a-contract"})
    assert scheduler.active_count == scheduler.waiting_count == 0


@pytest.mark.asyncio
async def test_context_manager_finishes_transferred_invocation_scope(tmp_path: Path) -> None:
    scheduler = ResourceScheduler()
    async with await scheduler.reserve([project_claim(tmp_path, lifetime="invocation")]) as lease:
        lease.transfer()
    assert lease.released and scheduler.active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("cancel_method", "cancel_count"), [("task", 1), ("task", 2), ("token", 1)])
async def test_cancel_during_final_auxiliary_cleanup_reclaims_undelivered_lease(
    tmp_path: Path, monkeypatch, cancel_method: str, cancel_count: int,
) -> None:
    scheduler = ResourceScheduler()
    original_create = asyncio.create_task
    pending = None
    auxiliary = []
    token = CancellationToken()

    def cancel_in_cleanup(_):
        if cancel_method == "token":
            token.cancel()
        else:
            pending.cancel()
            if cancel_count == 2:
                asyncio.get_running_loop().call_soon(pending.cancel)

    def traced_create(coroutine, *args, **kwargs):
        child = original_create(coroutine, *args, **kwargs)
        if getattr(coroutine, "__qualname__", "") == "CancellationToken.wait":
            # 授予已经完成，但 reserve 正在取消并等待辅助任务退出，尚未真正把租约交出去。
            auxiliary.append(child)
            child.add_done_callback(cancel_in_cleanup)
        return child

    monkeypatch.setattr(asyncio, "create_task", traced_create)
    pending = original_create(scheduler.reserve([path_claim(tmp_path / "x")],
        cancellation_token=token))
    try:
        result, = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)
        assert scheduler.active_count == scheduler.waiting_count == 0
        assert all(child.done() for child in auxiliary)
    finally:
        # 红灯运行也不能把重现出的租约留给后续测试。
        for lease in list(scheduler._active):
            await lease.release()
