from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.execution.contracts import ResourceClaim
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
