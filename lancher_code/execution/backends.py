from __future__ import annotations

import asyncio
import errno
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol


class ProcessBackend(Protocol):
    pid: int
    streams: tuple[str, ...]
    async def read(self, stream: str, size: int = 4096) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def wait(self) -> int: ...
    async def interrupt(self) -> None: ...
    async def terminate(self) -> None: ...
    async def resize(self, columns: int, rows: int) -> None: ...
    async def close(self) -> None: ...


async def spawn_backend(command: str, cwd: Path, *, transport: str = "pipe",
                        columns: int = 100, rows: int = 30) -> ProcessBackend:
    if transport not in {"pipe", "pty"}:
        raise ValueError("transport 只支持 pipe 或 pty。")
    if os.name == "nt":
        from lancher_code.execution.windows import WindowsBackend
        return await asyncio.to_thread(WindowsBackend.spawn, command, cwd,
                                       transport=transport, columns=columns, rows=rows)
    return await PosixBackend.spawn(command, cwd, transport=transport,
                                    columns=columns, rows=rows)


class PosixBackend:
    """每个命令拥有独立进程组；PTY 输出是单一终端流。"""

    def __init__(self, process: asyncio.subprocess.Process | None, *, master: int | None = None,
                 pid: int | None = None) -> None:
        self.process = process
        self.pid = process.pid if process else pid
        self.master = master
        self.streams = ("terminal",) if master is not None else ("stdout", "stderr")
        self._closed = False
        self._code: int | None = None
        self._io_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"pty-{self.pid}") if master is not None else None

    async def _io(self, operation):
        return await asyncio.get_running_loop().run_in_executor(self._io_executor, operation)

    @classmethod
    async def spawn(cls, command: str, cwd: Path, *, transport: str,
                    columns: int, rows: int) -> PosixBackend:
        if transport == "pipe":
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", command, cwd=str(cwd), start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            return cls(process)
        import fcntl
        import struct
        import termios
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            # setsid 在文件动作前发生；新 Session leader 打开 slave 时取得控制终端。
            # posix_spawn 不执行 Python preexec_fn，避免多线程程序在 fork 后碰到锁。
            slave_name = os.ttyname(slave)
            actions = [(os.POSIX_SPAWN_OPEN, 0, slave_name, os.O_RDWR, 0),
                       (os.POSIX_SPAWN_DUP2, 0, 1), (os.POSIX_SPAWN_DUP2, 0, 2)]
            import shlex
            payload = f"cd -- {shlex.quote(str(cwd))} && exec /bin/sh -c {shlex.quote(command)}"
            pid = await asyncio.to_thread(os.posix_spawn, "/bin/sh", ["/bin/sh", "-c", payload],
                                          os.environ.copy(), file_actions=actions, setsid=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(None, master=master, pid=pid)

    async def read(self, stream: str, size: int = 4096) -> bytes:
        if self.master is not None:
            try:
                return await self._io(lambda: os.read(self.master, size))
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return b""
                raise
        reader = self.process.stdout if stream == "stdout" else self.process.stderr
        return await reader.read(size) if reader else b""

    async def write(self, data: bytes) -> None:
        if self.master is not None:
            master = self.master
            def write_all() -> None:
                offset = 0
                while offset < len(data):
                    written = os.write(master, data[offset:])
                    if written <= 0:
                        raise BrokenPipeError("终端输入没有取得写入进展。")
                    offset += written
            await self._io(write_all)
        elif self.process.stdin:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def wait(self) -> int:
        if self.process is not None:
            return await self.process.wait()
        if self._code is None:
            _, status = await self._io(lambda: os.waitpid(self.pid, 0))
            self._code = os.waitstatus_to_exitcode(status)
        return self._code

    async def interrupt(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    async def terminate(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def resize(self, columns: int, rows: int) -> None:
        if self.master is None:
            raise ValueError("普通管道进程没有终端尺寸。")
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # 根进程退出后仍可能有子进程继承输出句柄，组级终止才能得到 EOF。
        await self.terminate()
        if self.process is not None and self.process.stdin:
            self.process.stdin.close()
        if self.master is not None:
            os.close(self.master)
            self.master = None
        if self._io_executor is not None:
            self._io_executor.shutdown(wait=False, cancel_futures=True)
