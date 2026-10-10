from __future__ import annotations

import asyncio
import codecs
import json
from datetime import datetime, timezone
from pathlib import Path

from lancher_code.execution.contracts import OutputPage
from lancher_code.sessions.paths import validate_control_path


class OutputLimitExceeded(RuntimeError):
    """日志配额到期后停止进程，不让持续输出耗尽磁盘。"""


class OutputStore:
    """字符游标读取追加日志；UTF8 解码、磁盘字节额度和返回字符预算分别计数。"""

    def __init__(self, directory: Path, *, project_root: Path,
                 max_bytes: int = 32 * 1024 * 1024) -> None:
        if max_bytes < 256:
            raise ValueError("输出日志配额至少为 256 字节。")
        self.project_root = project_root.resolve()
        self.directory = directory
        self.path = directory / "output.jsonl"
        self.index_path = directory / "output.index"
        self.max_bytes = max_bytes
        self._lock = asyncio.Lock()
        self._decoders = {name: codecs.getincrementaldecoder("utf-8")("replace")
                          for name in ("stdout", "stderr", "terminal")}
        self._offset = 0
        self._seq = 0
        self._chars = 0
        self._closed_streams: set[str] = set()
        for path in (directory, self.path, self.index_path):
            validate_control_path(self.project_root, path)
        directory.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=False)
        self.index_path.touch(exist_ok=False)

    @property
    def size_bytes(self) -> int:
        return self._offset

    @property
    def cursor(self) -> int:
        return self._chars

    async def append(self, stream: str, data: bytes, *, final: bool = False) -> None:
        if stream not in self._decoders:
            raise ValueError("未知输出流。")
        async with self._lock:
            if stream in self._closed_streams:
                return
            text = self._decoders[stream].decode(data, final=final)
            if final:
                self._closed_streams.add(stream)
            if not text:
                return
            record = {"seq": self._seq + 1, "stream": stream, "text": text,
                      "char_start": self._chars,
                      "timestamp": datetime.now(timezone.utc).isoformat()}
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if self._offset + len(encoded) > self.max_bytes:
                raise OutputLimitExceeded("进程输出达到磁盘配额，已请求停止。")
            validate_control_path(self.project_root, self.path)
            validate_control_path(self.project_root, self.index_path)
            write_task = asyncio.create_task(asyncio.to_thread(self._append_bytes, encoded))
            cancelled = False
            while not write_task.done():
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    # 写线程已经开始后不能丢掉游标提交；完成这一块，再传播取消。
                    cancelled = True
            write_task.result()
            self._seq += 1
            self._offset += len(encoded)
            self._chars += len(text)
            if cancelled:
                raise asyncio.CancelledError

    def _append_bytes(self, encoded: bytes) -> None:
        validate_control_path(self.project_root, self.path)
        with self.path.open("ab") as file:
            file.write(encoded)
            file.flush()
        if self._seq % 64 == 0:
            validate_control_path(self.project_root, self.index_path)
            with self.index_path.open("a", encoding="utf-8") as file:
                file.write(f"{self._chars} {self._offset}\n")

    def read(self, cursor: int = 0, *, max_chars: int = 12000) -> OutputPage:
        validate_control_path(self.project_root, self.path)
        validate_control_path(self.project_root, self.index_path)
        return read_saved_output(self.path, cursor, max_chars=max_chars, total_chars=self._chars)


def read_saved_output(path: Path, cursor: int = 0, *, max_chars: int = 12000,
                      total_chars: int | None = None) -> OutputPage:
    """字符游标允许在一个输出块中间停下，下一次读取不会丢掉被预算截断的尾部。"""
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("输出游标必须是非负整数。")
    if not 1 <= max_chars <= 100000:
        raise ValueError("每次读取预算必须在 1 至 100000 字符之间。")
    if total_chars is not None and cursor > total_chars:
        raise ValueError("输出游标超过已提交日志长度。")
    if not path.is_file():
        if cursor:
            raise ValueError("输出游标超过日志长度。")
        return OutputPage(cursor=0)
    offset = 0
    index = path.with_name("output.index")
    if index.is_file():
        with index.open(encoding="utf-8") as entries:
            for line in entries:
                try:
                    position, candidate = map(int, line.split())
                except ValueError:
                    continue
                if position <= cursor:
                    offset = candidate
                else:
                    break
    parts, stdout, stderr = [], [], []
    next_cursor = cursor
    end = 0
    remaining = max_chars
    with path.open("rb") as file:
        file.seek(offset)
        while line := file.readline():
            if not line.endswith(b"\n"):
                break
            record = json.loads(line)
            text = str(record["text"])
            start = int(record["char_start"])
            if total_chars is not None:
                if start >= total_chars:
                    break
                # 文件线程可能已写完，但逻辑游标尚未提交；只暴露已提交的字符。
                text = text[:total_chars - start]
            end = start + len(text)
            if end <= cursor:
                continue
            selected = text[max(cursor - start, 0):][:remaining]
            parts.append(selected)
            (stderr if record["stream"] == "stderr" else stdout).append(selected)
            remaining -= len(selected)
            next_cursor += len(selected)
            if not remaining:
                if total_chars is None:
                    for tail in file:
                        if tail.endswith(b"\n"):
                            record = json.loads(tail)
                            end = int(record["char_start"]) + len(str(record["text"]))
                break
    total = total_chars if total_chars is not None else end
    if cursor > total:
        raise ValueError("输出游标超过日志长度。")
    return OutputPage("".join(parts), "".join(stdout), "".join(stderr), cursor,
                      next_cursor, next_cursor < total)
