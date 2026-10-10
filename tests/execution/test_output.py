from __future__ import annotations

import json

import pytest

from lancher_code.execution.output import OutputLimitExceeded, OutputStore, read_saved_output


@pytest.mark.asyncio
async def test_character_cursor_partial_chunk_and_independent_consumers(tmp_path):
    store = OutputStore(tmp_path / "process", project_root=tmp_path)
    await store.append("stdout", "甲乙丙丁戊".encode())
    await store.append("stderr", "错误".encode())
    first = store.read(max_chars=2)
    assert first.text == "甲乙" and first.next_cursor == 2 and first.truncated
    second = store.read(first.next_cursor, max_chars=3)
    assert second.text == "丙丁戊" and second.next_cursor == 5
    third = store.read(second.next_cursor, max_chars=2)
    assert third.text == "错误" and third.stderr == "错误" and not third.truncated
    assert store.read(max_chars=7).text == "甲乙丙丁戊错误"
    assert read_saved_output(store.path, 1, max_chars=3).text == "乙丙丁"


@pytest.mark.asyncio
async def test_incremental_utf8_decoder_separates_streams_and_flushes_partial_bytes(tmp_path):
    store = OutputStore(tmp_path / "process", project_root=tmp_path)
    data = "你好".encode()
    await store.append("stdout", data[:2])
    await store.append("stderr", b"error")
    await store.append("stdout", data[2:])
    await store.append("stdout", b"\xe4", final=True)
    page = store.read()
    assert page.stdout == "你好�" and page.stderr == "error"
    assert page.text == "error你好�"
    await store.append("stdout", b"ignored")
    assert store.read().text == page.text


@pytest.mark.asyncio
async def test_disk_quota_stops_before_exceeding_limit(tmp_path):
    store = OutputStore(tmp_path / "process", project_root=tmp_path, max_bytes=256)
    await store.append("stdout", b"saved")
    saved_size = store.size_bytes
    with pytest.raises(OutputLimitExceeded):
        await store.append("stdout", b"x" * 200)
    assert store.size_bytes == saved_size <= 256
    assert store.read().text == "saved"


@pytest.mark.asyncio
async def test_sparse_index_can_resume_at_middle_of_later_chunk(tmp_path):
    store = OutputStore(tmp_path / "process", project_root=tmp_path)
    for _ in range(150):
        await store.append("stdout", b"abc")
    assert store.read(389, max_chars=4).text == "cabc"
    assert read_saved_output(store.path, 389, max_chars=4).next_cursor == 393
    assert len(store.index_path.read_text().splitlines()) == 3
    assert json.loads(store.path.read_text().splitlines()[64])["char_start"] == 192


@pytest.mark.parametrize("cursor", [-1, True, "1", 100])
@pytest.mark.asyncio
async def test_invalid_cursors_rejected(tmp_path, cursor):
    store = OutputStore(tmp_path / "process", project_root=tmp_path)
    await store.append("stdout", b"hello")
    with pytest.raises(ValueError):
        store.read(cursor)


@pytest.mark.asyncio
async def test_reader_never_observes_uncommitted_worker_append(tmp_path, monkeypatch):
    import asyncio
    import threading
    store = OutputStore(tmp_path / "process", project_root=tmp_path)
    written = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store._append_bytes
    def gated(encoded):
        original(encoded)
        loop.call_soon_threadsafe(written.set)
        release.wait(5)
    monkeypatch.setattr(store, "_append_bytes", gated)
    appending = asyncio.create_task(store.append("stdout", b"hello"))
    try:
        await asyncio.wait_for(written.wait(), 5)
        assert store.read().text == "" and store.read().next_cursor == 0
        release.set()
        await appending
        assert store.read().text == "hello" and store.read().next_cursor == 5
    finally:
        release.set()
        await appending
