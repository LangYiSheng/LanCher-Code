from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from lancher_code.execution.contracts import ExecutionLimits, ProcessSpec, ReadinessProbe
from lancher_code.execution.processes import ProcessSupervisor
from lancher_code.models import CancellationToken


def command(script: str) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in (sys.executable, "-c", script))
    return shlex.join((sys.executable, "-c", script))


def supervisor(root: Path, **limits):
    return ProcessSupervisor(root, limits=ExecutionLimits(stop_grace_seconds=.05,
                             drain_timeout_seconds=1, **limits))


async def start(manager, root, script, *, session_id=None, turn_id="turn", **spec):
    sid = session_id or uuid4().hex
    info = await manager.start(ProcessSpec(command(script), "测试进程", root, **spec),
                               session_id=sid, turn_id=turn_id, invocation_id=uuid4().hex)
    return info, sid


@pytest.mark.asyncio
async def test_short_command_unicode_and_nonzero_exit(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "import sys;print('你好');sys.exit(3)", yield_ms=5000)
        assert info.status == "exited" and info.exit_code == 3
        assert "你好" in manager.read(info.process_id, sid).stdout
        assert manager.get(info.process_id, sid).status == "exited"
        with pytest.raises(ValueError):
            manager.get(info.process_id, uuid4().hex)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_yield_does_not_terminate_and_lifetime_scopes(tmp_path):
    manager = supervisor(tmp_path)
    sid = uuid4().hex
    try:
        foreground, _ = await start(manager, tmp_path, "import time;time.sleep(30)", session_id=sid, yield_ms=0)
        background, _ = await start(manager, tmp_path, "import time;time.sleep(30)", session_id=sid,
                                    lifetime="session", yield_ms=0)
        assert foreground.status == background.status == "running"
        assert (await manager.wait(foreground.process_id, sid, timeout_ms=10)).status == "running"
        await manager.stop_turn(sid, "turn")
        assert manager.get(foreground.process_id, sid).status == "cancelled"
        assert manager.get(background.process_id, sid).status == "running"
        await manager.stop_session(sid)
        assert not manager.active_session(sid)
    finally:
        await manager.close()


@pytest.mark.parametrize("scope", ["turn", "session"])
@pytest.mark.asyncio
async def test_cancel_stop_batch_before_children_start_reaps_all_and_reopens_scope(tmp_path, monkeypatch, scope):
    """批次已经封住入口，单个停止任务还未运行时，取消也不能漏掉整个进程组。"""
    manager = supervisor(tmp_path)
    sid = uuid4().hex
    batch_started, children_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered = []
    stopping = None
    seal_name = "seal_turn" if scope == "turn" else "seal_session"
    original_seal, original_stop = getattr(manager, seal_name), manager.stop

    def record_seal(*args):
        original_seal(*args)
        # 唤醒观察者后，批次才创建 stop 子任务，精确覆盖首次运行前的取消窗口。
        batch_started.set()

    async def gated_stop(process_id, session_id, **kwargs):
        entered.append(process_id)
        if len(entered) == 2:
            children_started.set()
        await release.wait()
        return await original_stop(process_id, session_id, **kwargs)

    monkeypatch.setattr(manager, seal_name, record_seal)
    monkeypatch.setattr(manager, "stop", gated_stop)
    try:
        processes = []
        for _ in range(2):
            info, _ = await start(manager, tmp_path, "import time;time.sleep(30)", session_id=sid,
                                  lifetime=scope, yield_ms=0)
            processes.append(info)
        operation = manager.stop_turn(sid, "turn") if scope == "turn" else manager.stop_session(sid)
        stopping = asyncio.create_task(operation)
        await asyncio.wait_for(batch_started.wait(), 2)
        assert not entered
        stopping.cancel()
        await asyncio.wait_for(children_started.wait(), 2)
        # 重复取消仍需等待被托管的整批清理，Session 入口不能提前重开。
        stopping.cancel()
        assert not stopping.done()
        with pytest.raises(RuntimeError, match="执行范围已停止"):
            await start(manager, tmp_path, "print('too-early')", session_id=sid, yield_ms=0)
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert not stopping.cancelled()
        assert not manager.active_session(sid)
        assert {manager.get(info.process_id, sid).status for info in processes} == {"cancelled"}
        assert sid not in manager._stopping_sessions
        # turn 的旧 UUID 保持封闭；同 Session 的新轮次或完整停止后的 Session 可继续使用。
        next_turn = "next" if scope == "turn" else "turn"
        restarted, _ = await start(manager, tmp_path, "print('reopened')", session_id=sid,
                                   turn_id=next_turn, yield_ms=5000)
        assert restarted.status == "exited" and restarted.exit_code == 0
        assert "reopened" in manager.read(restarted.process_id, sid).text
    finally:
        release.set()
        await manager.close()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_transfer_and_idempotent_stop(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0)
        assert (await manager.background(info.process_id, sid)).lifetime == "session"
        await manager.stop_turn(sid, "turn")
        assert manager.get(info.process_id, sid).status == "running"
        first, second = await asyncio.gather(manager.stop(info.process_id, sid), manager.stop(info.process_id, sid))
        assert first.status == second.status == "cancelled"
        assert (await manager.stop(info.process_id, sid)).exit_code == first.exit_code
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_runtime_limit_distinct_from_yield(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0, max_runtime_ms=100)
        ended = await manager.wait(info.process_id, sid, timeout_ms=5000)
        assert ended.status == "failed" and ended.exit_reason == "runtime_limit"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_output_quota_stops_and_preserves_saved_prefix(tmp_path):
    manager = supervisor(tmp_path, output_limit_bytes=500)
    try:
        info, sid = await start(manager, tmp_path, "import time;print('x'*10000,flush=True);time.sleep(30)", yield_ms=5000)
        assert info.status == "failed" and info.exit_reason == "output_limit"
        assert manager.read(info.process_id, sid).next_cursor <= 500
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_read_write_stdin_and_readiness(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "print(input(),flush=True)", yield_ms=0)
        await manager.write(info.process_id, sid, "输入测试\n")
        assert (await manager.wait(info.process_id, sid, timeout_ms=5000)).exit_code == 0
        assert "输入测试" in manager.read(info.process_id, sid).text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_start_event_failure_never_spawns(tmp_path, monkeypatch):
    started = False
    async def forbidden_spawn(*args, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("不能启动")
    monkeypatch.setattr("lancher_code.execution.processes.spawn_backend", forbidden_spawn)
    manager = ProcessSupervisor(tmp_path, event_sink=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        await start(manager, tmp_path, "print(1)")
    assert not started


@pytest.mark.asyncio
async def test_event_persistence_failure_does_not_block_process_cleanup(tmp_path):
    fail = False
    def sink(*args, **kwargs):
        if fail:
            raise OSError("disk")
    manager = ProcessSupervisor(tmp_path, limits=ExecutionLimits(stop_grace_seconds=.05), event_sink=sink)
    info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0)
    fail = True
    stopped = await asyncio.wait_for(manager.stop(info.process_id, sid), 5)
    assert stopped.status == "cancelled" and not manager.active_session(sid)
    await manager.close()


@pytest.mark.parametrize("repeat", [1, 2])
@pytest.mark.asyncio
async def test_cancel_during_spawn_obtains_handle_then_reaps(tmp_path, monkeypatch, repeat):
    from lancher_code.execution import processes
    original = processes.spawn_backend
    created, release = asyncio.Event(), asyncio.Event()
    backend = None
    async def gated(*args, **kwargs):
        nonlocal backend
        backend = await original(*args, **kwargs)
        created.set()
        await release.wait()
        return backend
    monkeypatch.setattr(processes, "spawn_backend", gated)
    manager = supervisor(tmp_path)
    sid = uuid4().hex
    task = asyncio.create_task(manager.start(ProcessSpec(command("import time;time.sleep(30)"), "取消", tmp_path),
                                            session_id=sid, turn_id="turn", invocation_id="invoke"))
    try:
        await asyncio.wait_for(created.wait(), 5)
        for _ in range(repeat):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not manager.active_session(sid)
        assert await backend.wait() != 0
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_token_cancel_during_yield_reaps(tmp_path):
    manager = supervisor(tmp_path)
    token = CancellationToken()
    sid = uuid4().hex
    task = asyncio.create_task(manager.start(ProcessSpec(command("import time;time.sleep(30)"), "取消", tmp_path, yield_ms=5000),
        session_id=sid, turn_id="turn", invocation_id="invoke", cancellation_token=token))
    await asyncio.sleep(.2)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not manager.active_session(sid)
    await manager.close()


@pytest.mark.asyncio
async def test_restart_marks_records_lost_without_pid_adoption(tmp_path):
    manager = supervisor(tmp_path)
    info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0)
    restarted = supervisor(tmp_path)
    assert restarted.get(info.process_id, sid).status == "lost"
    assert restarted.list(sid)[0].exit_reason == "application_restarted"
    assert not restarted.active_session(sid)
    await manager.close()


@pytest.mark.parametrize("supports_binding", [False, True])
@pytest.mark.asyncio
async def test_process_capacity_and_resource_lease_retained_until_exit(tmp_path, supports_binding):
    manager = supervisor(tmp_path, max_processes_per_session=1)
    sid = uuid4().hex
    class Lease:
        transferred = False
        released = False
        process_id = None
        def transfer(self):
            self.transferred = True
        async def release(self):
            self.released = True
    lease = Lease()
    if supports_binding:
        lease.bind_process = lambda process_id: setattr(lease, "process_id", process_id)
    try:
        info = await manager.start(ProcessSpec(command("import time;time.sleep(30)"), "锁", tmp_path, yield_ms=0),
            session_id=sid, turn_id="turn", invocation_id="invoke", resource_lease=lease)
        assert lease.transferred and not lease.released
        assert lease.process_id == (info.process_id if supports_binding else None)
        with pytest.raises(RuntimeError):
            await start(manager, tmp_path, "print(1)", session_id=sid)
        await manager.stop(info.process_id, sid)
        assert lease.released
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_stop_process_tree_with_inherited_pipes(tmp_path):
    manager = supervisor(tmp_path)
    pid_file = tmp_path / "child.pid"
    script = ("import subprocess,sys,time,pathlib;"
              "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
              f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)+'\\n');time.sleep(30)")
    try:
        info, sid = await start(manager, tmp_path, script, yield_ms=0)
        child = None
        for _ in range(100):
            try:
                raw_pid = pid_file.read_text()
            except FileNotFoundError:
                raw_pid = ""
            # 文件创建先于写入；末尾换行代表完整记录，不能把空内容或半个 PID 当成就绪。
            if raw_pid.endswith("\n") and raw_pid.strip().isdecimal() and int(raw_pid.strip()) > 0:
                child = int(raw_pid.strip())
                break
            await asyncio.sleep(.05)
        assert child is not None, "子进程没有及时保存完整 PID。"
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x00100000, False, child)
            assert handle
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        ended = await asyncio.wait_for(manager.stop(info.process_id, sid), 5)
        assert ended.status == "cancelled"
        if os.name == "nt":
            try:
                assert kernel.WaitForSingleObject(handle, 1000) == 0
            finally:
                kernel.CloseHandle(handle)
        else:
            # 某些容器的 PID 1 不立即回收孤儿 zombie，至少不能仍然执行。
            status = Path(f"/proc/{child}/stat")
            assert not status.exists() or ") Z " in status.read_text()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_readiness_tcp_probe_reports_ready(tmp_path):
    manager = supervisor(tmp_path)
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0,
                                readiness=ReadinessProbe(port=port, timeout_ms=1000))
        for _ in range(100):
            if manager.get(info.process_id, sid).readiness == "ready":
                break
            await asyncio.sleep(.02)
        assert manager.get(info.process_id, sid).readiness == "ready"
    finally:
        server.close()
        await server.wait_closed()
        await manager.close()


@pytest.mark.asyncio
async def test_stop_session_during_spawn_waits_for_handle_and_reaps(tmp_path, monkeypatch):
    from lancher_code.execution import processes
    original = processes.spawn_backend
    created, release = asyncio.Event(), asyncio.Event()
    async def gated(*args, **kwargs):
        backend = await original(*args, **kwargs)
        created.set()
        await release.wait()
        return backend
    monkeypatch.setattr(processes, "spawn_backend", gated)
    manager = supervisor(tmp_path)
    sid = uuid4().hex
    starting = asyncio.create_task(manager.start(
        ProcessSpec(command("import time;time.sleep(30)"), "创建期间停止", tmp_path, yield_ms=0),
        session_id=sid, turn_id="turn", invocation_id="invoke"))
    try:
        await asyncio.wait_for(created.wait(), 5)
        stopping = asyncio.create_task(manager.stop_session(sid))
        await asyncio.sleep(.05)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 5)
        info = await asyncio.wait_for(starting, 5)
        assert info.status == "cancelled" and not manager.active_session(sid)
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_pty_supervisor_shutdown_and_output(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "print('PTY-SUPERVISED',flush=True)",
                                transport="pty", yield_ms=5000)
        assert info.status == "exited" and info.exit_code == 0
        assert "PTY-SUPERVISED" in manager.read(info.process_id, sid).text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_readiness_timeout_does_not_claim_process_is_ready(tmp_path):
    manager = supervisor(tmp_path)
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0,
                                readiness=ReadinessProbe(port=port, timeout_ms=100))
        for _ in range(100):
            if manager.get(info.process_id, sid).readiness == "timeout":
                break
            await asyncio.sleep(.02)
        latest = manager.get(info.process_id, sid)
        assert latest.readiness == "timeout" and latest.status == "running"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_natural_root_exit_reaps_children_holding_output_pipes(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path,
            "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print('ROOT-EXIT')",
            yield_ms=5000)
        assert info.status == "exited" and info.exit_reason == "completed"
        assert "ROOT-EXIT" in manager.read(info.process_id, sid).text
        assert not manager.active_session(sid)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_blocked_stdin_stops_target_before_waiting_for_write(tmp_path, monkeypatch):
    from lancher_code.execution import processes
    class Backend:
        pid = 123
        streams = ()
        def __init__(self):
            self.writing = asyncio.Event()
            self.exited = asyncio.Event()
        async def write(self, data):
            self.writing.set()
            await self.exited.wait()
            raise BrokenPipeError("stopped")
        async def wait(self):
            await self.exited.wait()
            return 1
        async def interrupt(self):
            # 模拟没有控制台且不会主动退出的进程。
            pass
        async def terminate(self):
            self.exited.set()
        async def close(self):
            self.exited.set()
    backend = Backend()
    async def spawn(*args, **kwargs):
        return backend
    monkeypatch.setattr(processes, "spawn_backend", spawn)
    manager = supervisor(tmp_path)
    info, sid = await start(manager, tmp_path, "print(1)", yield_ms=0, lifetime="session")
    writing = asyncio.create_task(manager.write(info.process_id, sid, "x" * 65536))
    await backend.writing.wait()
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(writing, 2)
    latest = manager.get(info.process_id, sid)
    assert latest.status == "cancelled" and latest.exit_reason == "input_cancelled"
    await manager.close()


@pytest.mark.asyncio
async def test_sealing_prevents_late_background_and_input_before_cleanup(tmp_path):
    manager = supervisor(tmp_path)
    sid = uuid4().hex
    try:
        info, _ = await start(manager, tmp_path, "import time;time.sleep(30)", session_id=sid, yield_ms=0)
        manager.seal_turn(sid, "turn")
        with pytest.raises(ValueError):
            await manager.background(info.process_id, sid)
        with pytest.raises(ValueError):
            await manager.write(info.process_id, sid, "late")
        with pytest.raises(RuntimeError):
            await start(manager, tmp_path, "print(1)", session_id=sid)
        background, _ = await start(manager, tmp_path, "import time;time.sleep(30)", session_id=sid,
                                    turn_id="next", lifetime="session", yield_ms=0)
        manager.seal_session(sid)
        with pytest.raises(ValueError):
            await manager.background(background.process_id, sid)
        with pytest.raises(ValueError):
            await manager.write(background.process_id, sid, "late")
        with pytest.raises(RuntimeError):
            await start(manager, tmp_path, "print(1)", session_id=sid, turn_id="new")
        await manager.stop_session(sid)
        assert not manager.active_session(sid)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_real_blocked_input_cancellation_is_bounded(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0)
        writing = asyncio.create_task(manager.write(info.process_id, sid, "x" * 65536))
        await asyncio.sleep(.2)
        if not writing.done():
            writing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(writing, 5)
            assert manager.get(info.process_id, sid).exit_reason == "input_cancelled"
        else:
            # POSIX 管道可能接受到内核缓冲区，此时取消没有未完成的写入可处理。
            await writing
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_io_readers_cannot_starve_default_thread_pool_and_new_spawn(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    tiny_pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(tiny_pool)
    manager = supervisor(tmp_path)
    sid = uuid4().hex
    try:
        for index in range(2):
            info, _ = await asyncio.wait_for(start(manager, tmp_path, "import time;time.sleep(30)",
                session_id=sid, turn_id=str(index), lifetime="session", yield_ms=0), 5)
            assert info.status == "running"
        short, _ = await asyncio.wait_for(start(manager, tmp_path, "print('NOT-STARVED')",
            session_id=sid, turn_id="short", yield_ms=5000), 7)
        assert short.exit_code == 0 and "NOT-STARVED" in manager.read(short.process_id, sid).text
    finally:
        await manager.close()
        if previous is not None:
            loop.set_default_executor(previous)
        else:
            # 保留一个空闲默认池供本次测试事件循环正常关闭。
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        tiny_pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_readiness_event_failure_stops_without_recursive_cleanup(tmp_path):
    def sink(session_id, kind, data, **kwargs):
        if kind in {"process.ready", "process.stopping", "process.exited"}:
            raise OSError("storage unavailable")
    manager = ProcessSupervisor(tmp_path, limits=ExecutionLimits(stop_grace_seconds=.05), event_sink=sink)
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0,
                                readiness=ReadinessProbe(port=port, timeout_ms=1000))
        ended = await manager.wait(info.process_id, sid, timeout_ms=5000)
        assert ended.status == "failed" and ended.exit_reason == "event_error"
        assert ended.storage_error == "storage unavailable"
        assert not manager.active_session(sid)
    finally:
        server.close()
        await server.wait_closed()
        await manager.close()


@pytest.mark.asyncio
async def test_input_event_only_contains_successful_byte_count(tmp_path):
    events = []
    manager = ProcessSupervisor(tmp_path, limits=ExecutionLimits(stop_grace_seconds=.05),
        event_sink=lambda session_id, kind, data, **kwargs: events.append((kind, data)))
    try:
        info, sid = await start(manager, tmp_path, "input();print('RECEIVED')", yield_ms=0)
        await manager.write(info.process_id, sid, "private-secret\n")
        sent = next(data for kind, data in events if kind == "process.input_sent")
        assert sent["input_bytes"] == len(b"private-secret\n") and sent["input_status"] == "sent"
        assert "private-secret" not in json.dumps(sent)
        await manager.wait(info.process_id, sid, timeout_ms=5000)
    finally:
        await manager.close()


@pytest.mark.parametrize("cache_kind", ["stale", "damaged", "missing"])
@pytest.mark.asyncio
async def test_authoritative_session_projection_overrides_process_cache(tmp_path, cache_kind):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "print('DONE')", yield_ms=5000)
        assert info.status == "exited"
        cache = tmp_path / ".lancher" / "sessions" / sid / "processes" / info.process_id / "meta.json"
        if cache_kind == "stale":
            cache.write_text(json.dumps(dict(info.to_dict(), status="running", exit_code=None)), encoding="utf-8")
        elif cache_kind == "damaged":
            cache.write_text("{broken", encoding="utf-8")
        else:
            cache.unlink()
        restarted = ProcessSupervisor(tmp_path, saved_processes=lambda session_id: [info.to_dict()])
        assert restarted.get(info.process_id, sid).status == "exited"
        assert restarted.list(sid)[0].status == "exited"
        assert not restarted.active_session(sid)
        assert "DONE" in restarted.read(info.process_id, sid).text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_authoritative_unknown_running_record_is_lost_without_pid_adoption(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0)
        restarted = ProcessSupervisor(tmp_path, saved_processes=lambda session_id: [info.to_dict()])
        assert restarted.get(info.process_id, sid).status == "lost"
        assert restarted.list(sid)[0].readiness == "unknown"
        assert not restarted.active_session(sid)
        assert (await restarted.stop(info.process_id, sid)).status == "lost"
        # 只恢复历史记录，重启实例的 stop 不会把旧 PID 当成可控制对象。
        assert manager.get(info.process_id, sid).status == "running"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_process_state_wins_over_saved_projection(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "import time;time.sleep(30)", yield_ms=0)
        manager.saved_processes = lambda session_id: [dict(info.to_dict(), status="exited")]
        assert manager.get(info.process_id, sid).status == "running"
        assert manager.list(sid)[0].status == "running"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_authoritative_projection_rejects_cross_session_identity(tmp_path):
    manager = supervisor(tmp_path)
    try:
        info, sid = await start(manager, tmp_path, "print('DONE')", yield_ms=5000)
        restarted = ProcessSupervisor(tmp_path,
            saved_processes=lambda session_id: [dict(info.to_dict(), session_id=uuid4().hex)])
        with pytest.raises(ValueError, match="归属"):
            restarted.get(info.process_id, sid)
        with pytest.raises(ValueError, match="归属"):
            restarted.list(sid)
    finally:
        await manager.close()
