from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class FileState:
    path: Path
    mtime_ns: int | None = None
    content: str | None = None
    was_read: bool = False
    is_complete: bool = False


class FileStateCache:
    def __init__(self) -> None:
        self._entries: dict[Path, FileState] = {}

    def get(self, path: Path) -> FileState | None:
        return self._entries.get(path.resolve())

    def record_read(self, path: Path, content: str, *, mtime_ns: int, is_complete: bool) -> FileState:
        resolved = path.resolve()
        state = FileState(
            path=resolved,
            mtime_ns=mtime_ns,
            content=content,
            was_read=True,
            is_complete=is_complete,
        )
        self._entries[resolved] = state
        return state

    def record_write(self, path: Path, *, mtime_ns: int) -> FileState:
        resolved = path.resolve()
        previous = self._entries.get(resolved)
        state = FileState(
            path=resolved,
            mtime_ns=mtime_ns,
            content=None,
            was_read=previous.was_read if previous is not None else False,
            is_complete=False,
        )
        self._entries[resolved] = state
        return state
