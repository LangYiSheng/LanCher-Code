from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from lancher_code.models import CancellationToken, ToolContext
from lancher_code.tools.builtin.read_file import ReadFileTool
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.core import common


async def _context_after_read(path: Path) -> ToolContext:
    context = ToolContext(cwd=path.parent, timeout_seconds=1, cancellation_token=CancellationToken())
    assert (await ReadFileTool().execute({"path": path.name}, context)).ok
    return context


@pytest.mark.asyncio
async def test_replace_failure_keeps_original_and_removes_staging_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    context = await _context_after_read(target)

    def fail_replace(*_):
        raise OSError("不能提交")

    monkeypatch.setattr(common.os, "replace", fail_replace)
    result = await WriteFileTool().execute({"path": target.name, "content": "new"}, context)
    assert result.error_code == "write_error"
    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_stop_before_replace_keeps_original(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    context = await _context_after_read(target)
    real_fsync = os.fsync

    def stop_after_staging(fd):
        real_fsync(fd)
        context.cancellation_token.cancel()

    monkeypatch.setattr(common.os, "fsync", stop_after_staging)
    with pytest.raises(asyncio.CancelledError):
        await WriteFileTool().execute({"path": target.name, "content": "new"}, context)
    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_external_change_during_staging_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    context = await _context_after_read(target)
    old_mtime = target.stat().st_mtime_ns
    real_fsync = os.fsync

    def update_after_staging(fd):
        real_fsync(fd)
        target.write_text("external", encoding="utf-8")
        os.utime(target, ns=(old_mtime + 1000000000, old_mtime + 1000000000))

    monkeypatch.setattr(common.os, "fsync", update_after_staging)
    result = await WriteFileTool().execute({"path": target.name, "content": "new"}, context)
    assert result.error_code == "file_changed_since_read"
    assert target.read_text(encoding="utf-8") == "external"
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_concurrent_creation_is_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "new.txt"
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    real_fsync = os.fsync

    def create_after_staging(fd):
        real_fsync(fd)
        target.write_text("external", encoding="utf-8")

    monkeypatch.setattr(common.os, "fsync", create_after_staging)
    result = await WriteFileTool().execute({"path": target.name, "content": "new"}, context)
    assert result.error_code == "file_changed_since_read"
    assert target.read_text(encoding="utf-8") == "external"
