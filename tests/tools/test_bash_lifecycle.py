from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.models import CancellationToken, ToolContext
from lancher_code.tools.builtin.bash import BashTool, POWERSHELL


@pytest.mark.skipif(not Path(POWERSHELL).is_file(), reason="需要 Windows PowerShell 子进程")
@pytest.mark.parametrize("stop_kind", ["timeout", "timeout_token", "token", "task", "task_token"])
@pytest.mark.asyncio
async def test_bash_stop_reaps_process_and_drains_both_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_kind: str,
) -> None:
    create_subprocess = asyncio.create_subprocess_exec
    created = asyncio.Event()
    processes = []

    async def capture_process(*args, **kwargs):
        process = await create_subprocess(*args, **kwargs)
        processes.append(process)
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    token = CancellationToken() if "token" in stop_kind else None
    task = asyncio.create_task(BashTool().execute(
        {"description": "等待取消测试", "command": "Start-Sleep -Seconds 30"},
        ToolContext(cwd=tmp_path, timeout_seconds=0.05 if stop_kind.startswith("timeout") else 10,
            cancellation_token=token),
    ))
    await asyncio.wait_for(created.wait(), 5)
    if stop_kind == "token":
        token.cancel()
    elif stop_kind.startswith("task"):
        task.cancel()

    result = None
    error = None
    try:
        try:
            result = await asyncio.wait_for(task, 5)
        except BaseException as exc:
            error = exc
        process = processes[0]
        # 工具返回或抛出取消前必须完成收尾，不能依赖测试清理或后续 GC。
        reaped = process.returncode is not None
        drained = process.stdout.at_eof() and process.stderr.at_eof()
    finally:
        # 旧实现失败时仍清理测试子进程，避免复现本身遗留句柄。
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.communicate(), 5)

    if stop_kind.startswith("timeout"):
        assert error is None, f"超时不应变成取消或进程错误：{error!r}"
        assert result is not None and result.error_code == "command_timeout"
    else:
        assert isinstance(error, asyncio.CancelledError), repr(error)
    assert reaped, "子进程尚未回收"
    assert drained, "stdout/stderr 管道读取被取消，未等待 EOF"


@pytest.mark.skipif(not Path(POWERSHELL).is_file(), reason="需要 Windows PowerShell 子进程")
@pytest.mark.parametrize("cancellations", [1, 2])
@pytest.mark.asyncio
async def test_bash_cancel_during_spawn_waits_for_handle_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellations: int,
) -> None:
    create_subprocess = asyncio.create_subprocess_exec
    created = asyncio.Event()
    release_handle = asyncio.Event()
    processes = []

    async def gated_spawn(*args, **kwargs):
        process = await create_subprocess(*args, **kwargs)
        processes.append(process)
        created.set()
        await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    task = asyncio.create_task(BashTool().execute(
        {"description": "创建期间取消", "command": "Start-Sleep -Seconds 30"},
        ToolContext(cwd=tmp_path, timeout_seconds=10),
    ))
    await asyncio.wait_for(created.wait(), 5)
    try:
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
        finished_before_handle = task.done()
        release_handle.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        process = processes[0]
        reaped = process.returncode is not None
        drained = process.stdout.at_eof() and process.stderr.at_eof()
    finally:
        release_handle.set()
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.communicate(), 5)

    assert not finished_before_handle, "取消提前退出，尚未获得句柄的真实进程无人收尾"
    assert reaped and drained


@pytest.mark.skipif(not Path(POWERSHELL).is_file(), reason="需要 Windows PowerShell 子进程")
@pytest.mark.asyncio
async def test_bash_repeated_cancel_cannot_interrupt_pipe_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    create_subprocess = asyncio.create_subprocess_exec
    reading = asyncio.Event()
    release_read = asyncio.Event()
    drained_event = asyncio.Event()
    processes = []

    async def gated_spawn(*args, **kwargs):
        process = await create_subprocess(*args, **kwargs)
        processes.append(process)
        communicate = process.communicate

        async def gated_communicate():
            reading.set()
            await release_read.wait()
            result = await communicate()
            drained_event.set()
            return result

        process.communicate = gated_communicate
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    task = asyncio.create_task(BashTool().execute(
        {"description": "连续停止期间清理", "command": "Start-Sleep -Seconds 30"},
        ToolContext(cwd=tmp_path, timeout_seconds=10),
    ))
    await asyncio.wait_for(reading.wait(), 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        finished_before_drain = task.done()
        release_read.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        process = processes[0]
        reaped = process.returncode is not None
        drained = process.stdout.at_eof() and process.stderr.at_eof()
    finally:
        release_read.set()
        await asyncio.wait_for(drained_event.wait(), 5)

    assert not finished_before_drain, "第二次取消打断了仍在进行的管道清理"
    assert reaped and drained
