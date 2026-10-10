from __future__ import annotations

import asyncio
import os
import shlex
import sys

import pytest

from lancher_code.execution.backends import spawn_backend


def python_command(script: str) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in (sys.executable, "-c", script))
    return shlex.join((sys.executable, "-c", script))


async def drain(backend, stream):
    chunks = []
    while chunk := await backend.read(stream):
        chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_pipe_backend_stdout_stderr_stdin_and_reap(tmp_path):
    backend = await spawn_backend(python_command("import sys; print(input()); print('error',file=sys.stderr)"), tmp_path)
    readers = {stream: asyncio.create_task(drain(backend, stream)) for stream in backend.streams}
    try:
        await backend.write(b"hello\n")
        code = await asyncio.wait_for(backend.wait(), 10)
        await asyncio.wait_for(asyncio.gather(*readers.values()), 5)
        assert code == 0
        assert b"hello" in readers["stdout"].result()
        assert b"error" in readers["stderr"].result()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_pty_backend_terminal_output_and_resize(tmp_path):
    backend = await spawn_backend(python_command("print('terminal')"), tmp_path, transport="pty")
    reading = asyncio.create_task(drain(backend, "terminal"))
    try:
        await backend.resize(120, 40)
        assert await asyncio.wait_for(backend.wait(), 10) == 0
        finish = getattr(backend, "finish_group", backend.terminate)
        await asyncio.wait_for(finish(), 5)
        assert b"terminal" in await asyncio.wait_for(reading, 5)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_backend_invalid_transport(tmp_path):
    with pytest.raises(ValueError):
        await spawn_backend("echo x", tmp_path, transport="invalid")


@pytest.mark.asyncio
async def test_pty_interactive_input_stays_in_terminal(tmp_path):
    backend = await spawn_backend(python_command("import sys;print('READY',flush=True);print('VALUE='+input(),flush=True)"),
                                  tmp_path, transport="pty")
    reading = asyncio.create_task(drain(backend, "terminal"))
    try:
        await asyncio.sleep(.5)
        await backend.write(b"hello\r")
        assert await asyncio.wait_for(backend.wait(), 10) == 0
        finish = getattr(backend, "finish_group", backend.terminate)
        await asyncio.wait_for(finish(), 5)
        assert b"VALUE=hello" in await asyncio.wait_for(reading, 5)
    finally:
        await backend.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows 原生句柄清理")
@pytest.mark.asyncio
async def test_windows_close_failure_still_closes_job_and_kills_root(tmp_path, monkeypatch):
    import ctypes
    from lancher_code.execution.windows import kernel
    backend = await spawn_backend(python_command("import time;time.sleep(30)"), tmp_path)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    observation = kernel.OpenProcess(0x00100000, False, backend.pid)
    assert observation
    async def failing_finish():
        raise OSError("模拟终止错误")
    monkeypatch.setattr(backend, "finish_group", failing_finish)
    try:
        with pytest.raises(OSError):
            await backend.close()
        assert backend._job is None and backend._outputs == {} and backend._stdin is None
        assert kernel.WaitForSingleObject(observation, 2000) == 0
    finally:
        kernel.CloseHandle(observation)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job 关闭保障")
@pytest.mark.asyncio
async def test_windows_terminate_job_failure_uses_kill_on_close(tmp_path, monkeypatch):
    from lancher_code.execution.windows import kernel
    backend = await spawn_backend(python_command("import time;time.sleep(30)"), tmp_path)
    monkeypatch.setattr(kernel, "TerminateJobObject", lambda *args: False)
    try:
        await backend.terminate()
        assert backend._job is None
        # Windows KILL_ON_JOB_CLOSE 的退出码可能为 0；停止原因由 Supervisor 单独保存。
        assert isinstance(await asyncio.wait_for(backend.wait(), 5), int)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_posix_pty_write_retries_short_writes(monkeypatch):
    from lancher_code.execution.backends import PosixBackend
    received = bytearray()
    original = os.write
    fake_fd = 10_000_000
    def short_write(fd, data):
        if fd != fake_fd:
            return original(fd, data)
        length = min(3, len(data))
        received.extend(data[:length])
        return length
    monkeypatch.setattr(os, "write", short_write)
    backend = PosixBackend(None, master=fake_fd, pid=1)
    try:
        await backend.write("完整输入".encode())
        assert received == "完整输入".encode()
    finally:
        # 只有 fake fd，没有真实进程与终端句柄；回收本测试的工作线程即可。
        backend._io_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_posix_pty_zero_write_is_reported_as_failure(monkeypatch):
    from lancher_code.execution.backends import PosixBackend
    original = os.write
    fake_fd = 10_000_000
    monkeypatch.setattr(os, "write", lambda fd, data: 0 if fd == fake_fd else original(fd, data))
    backend = PosixBackend(None, master=fake_fd, pid=1)
    try:
        with pytest.raises(BrokenPipeError):
            await backend.write(b"input")
    finally:
        backend._io_executor.shutdown(wait=True)
