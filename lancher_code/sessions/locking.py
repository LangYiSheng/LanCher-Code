"""会话写入者的进程间文件锁。"""
from __future__ import annotations

import os
from typing import BinaryIO

from lancher_code.sessions.paths import SessionPaths
from lancher_code.sessions.storage import SessionBusyError, SessionRepositoryError, validate_storage_paths


class SessionFileLock:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self.stream: BinaryIO | None = None

    def acquire(self) -> None:
        validate_storage_paths(self.paths)
        try:
            self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
            validate_storage_paths(self.paths)
            stream = self.paths.lock.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise SessionBusyError("该 Session 正在使用，不能同时打开另一个写入者。") from exc
            self.stream = stream
        except SessionRepositoryError:
            raise
        except OSError as exc:
            raise SessionRepositoryError(f"无法锁定会话：{exc}") from exc

    def close(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
