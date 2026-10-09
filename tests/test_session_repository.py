from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from lancher_code.sessions import (
    ProjectSessionRepository, SessionBusyError, SessionPaths, SessionRepositoryError,
)
from lancher_code.sessions import repository as storage


def initial_data(**changes) -> dict:
    return {"state": {}, "messages": [], "transcript": [], "rules": [],
            "model_ref": None, **changes}


def create(repository, title="开发记录", **changes):
    return repository.create(uuid4().hex, title, initial_data(**changes))


def test_uuid_paths_are_immutable_and_project_scoped(tmp_path):
    session_id = uuid4().hex
    paths = SessionPaths.for_session(tmp_path, session_id)
    assert paths.root == tmp_path / ".lancher" / "sessions" / session_id
    assert paths.plan == paths.workspace / "plan.md"
    assert paths.blobs == paths.root / "blobs"
    assert paths.lock.parent == paths.root.parent / ".locks"
    with pytest.raises(FrozenInstanceError):
        paths.session_id = uuid4().hex


@pytest.mark.parametrize("session_id", ["", "../plan", "a" * 31, "g" * 32, "A" * 32, str(uuid4()), None])
def test_storage_rejects_noncanonical_uuid_before_creating_files(tmp_path, session_id):
    repository = ProjectSessionRepository(tmp_path)
    with pytest.raises(SessionRepositoryError, match="UUID"):
        repository.create(session_id, "标题", initial_data())
    assert not (tmp_path / ".lancher").exists()


def test_event_round_trip_metadata_and_rename_keep_stable_paths(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository, "可以包含 空格、标点 / 标题") as writer:
        session_id, paths = writer.session_id, writer.paths
        first = repository.read(session_id)
        assert len(first) == 1
        assert first[0]["type"] == "session.created"
        assert first[0]["data"]["initial_data"] == initial_data()
        event = writer.append("message.created", {"id": "u1", "role": "user", "content": "你好"}, "t1")
        assert event["turn_id"] == "t1"
        writer.append("message.created", {"id": "a1", "role": "assistant", "content": ""}, "t1")
        writer.append("message.updated", {"id": "a1", "fields": {"content": "回复"}}, "t1")
        writer.append("permissions.changed", {"rules": [{"match": "Read(*)"}]})
        writer.append("model.changed", {"model_ref": "provider/model"})
        writer.checkpoint({"last_message": "a1"})
        assert json.loads(paths.checkpoint.read_text(encoding="utf-8"))["last_seq"] == 6
        assert repository.list_sessions()[0].message_count == 2
    repository.rename(session_id, "新的 标题")
    repository.archive(session_id)
    info = repository.list_sessions()[0]
    assert info.session_id == session_id
    assert info.title == "新的 标题"
    assert info.archived
    assert info.permission_rule_count == 1
    assert info.model_ref == "provider/model"
    assert info.updated_at >= info.created_at
    assert paths.events.is_file()
    records = repository.read(session_id)
    assert [item["seq"] for item in records] == list(range(1, 9))
    assert all(item["version"] == 1 for item in records)


def test_two_sessions_have_separate_work_files_and_same_title_is_allowed(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository, "同名") as first, create(repository, "同名") as second:
        first.paths.plan.write_text("第一个计划", encoding="utf-8")
        second.paths.plan.write_text("第二个计划", encoding="utf-8")
        assert first.paths.plan.read_text(encoding="utf-8") == "第一个计划"
        assert first.session_id != second.session_id
        assert len(repository.list_sessions()) == 2


def test_duplicate_create_and_second_writer_cannot_override_existing_history(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    writer = create(repository)
    session_id = writer.session_id
    original = writer.paths.events.read_bytes()
    try:
        with pytest.raises(SessionBusyError):
            repository.open(session_id)
        with pytest.raises(SessionBusyError):
            repository.create(session_id, "其他", initial_data())
        with pytest.raises(SessionBusyError):
            repository.remove(session_id)
        assert repository.read(session_id)[0]["type"] == "session.created"
    finally:
        writer.close()
    with pytest.raises(SessionRepositoryError, match="已存在"):
        repository.create(session_id, "其他", initial_data())
    assert writer.paths.events.read_bytes() == original
    with repository.open(session_id) as resumed:
        assert resumed.append("state.changed", {"state": {}})["seq"] == 2


def test_os_lock_is_visible_to_another_python_process(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        script = """
import sys
from pathlib import Path
from lancher_code.sessions import ProjectSessionRepository, SessionBusyError
try:
    writer = ProjectSessionRepository(Path(sys.argv[1])).open(sys.argv[2])
except SessionBusyError:
    sys.exit(0)
writer.close()
sys.exit(3)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), writer.session_id],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("fragment", [b'{"version":1,"seq":2', b'\xff\x00', b'{"complete":"but not committed"}'])
def test_open_preserves_and_repairs_only_incomplete_tail(tmp_path, fragment):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        paths, session_id = writer.paths, writer.session_id
    before = paths.events.read_bytes()
    with paths.events.open("ab") as stream:
        stream.write(fragment)
    assert len(repository.read(session_id)) == 1
    assert paths.events.read_bytes() == before + fragment
    with repository.open(session_id) as writer:
        assert paths.events.read_bytes() == before
        saved = list((paths.root / "recovery").glob("*.bin"))
        assert len(saved) == 1 and saved[0].read_bytes() == fragment
        assert writer.append("state.changed", {"state": {}})["seq"] == 2
    assert len(repository.read(session_id)) == 2


@pytest.mark.parametrize("corruption", [b'not-json\n', b'{}\n', b'{"version":99}\n', b'\n'])
def test_complete_bad_line_is_reported_without_modifying_log(tmp_path, corruption):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        paths, session_id = writer.paths, writer.session_id
    with paths.events.open("ab") as stream:
        stream.write(corruption + b'{"trailing":')
    before = paths.events.read_bytes()
    for action in (repository.read, repository.open):
        with pytest.raises(SessionRepositoryError, match="第 2 行"):
            action(session_id)
    assert paths.events.read_bytes() == before
    assert not (paths.root / "recovery").exists()


def test_out_of_order_events_are_not_silently_recovered(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        paths, session_id = writer.paths, writer.session_id
        writer.append("state.changed", {"state": {}})
    lines = paths.events.read_text(encoding="utf-8").splitlines()
    changed = json.loads(lines[1])
    changed["seq"] = 4
    paths.events.write_text(lines[0] + "\n" + json.dumps(changed) + "\n", encoding="utf-8")
    with pytest.raises(SessionRepositoryError, match="序号"):
        repository.open(session_id)


def test_metadata_and_checkpoint_are_rebuildable_caches(tmp_path, monkeypatch):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        session_id, paths = writer.session_id, writer.paths
        def fail_cache(*args):
            raise OSError("缓存不可写")
        with monkeypatch.context() as patch:
            patch.setattr(storage, "_write_cache", fail_cache)
            writer.append("message.created", {"id": "m1", "role": "user", "content": "已持久化"})
            writer.checkpoint({"anything": True})
        assert len(repository.read(session_id)) == 2
    paths.metadata.write_text("坏缓存", encoding="utf-8")
    paths.checkpoint.write_text("坏缓存", encoding="utf-8")
    assert repository.list_sessions()[0].message_count == 1
    assert json.loads(paths.metadata.read_text(encoding="utf-8"))["last_seq"] == 2
    paths.metadata.unlink()
    assert repository.list_sessions()[0].message_count == 1


def test_append_is_durable_and_failed_fsync_blocks_further_writes(tmp_path, monkeypatch):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        sync_calls = []
        original = os.fsync
        def record(fd):
            sync_calls.append(fd)
            original(fd)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", record)
            writer.append("state.changed", {"state": {}})
        assert sync_calls
        def fail(fd):
            raise OSError("日志不可同步")
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail)
            with pytest.raises(SessionRepositoryError, match="日志不可同步"):
                writer.append("state.changed", {"state": {"changed": True}})
        with pytest.raises(SessionRepositoryError, match="重新打开"):
            writer.append("state.changed", {"state": {}})


def test_remove_is_locked_and_preserves_other_sessions_and_legacy_files(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    legacy = tmp_path / ".lancher" / "session" / "old.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("用户旧数据", encoding="utf-8")
    with create(repository) as first, create(repository) as second:
        first_id, first_paths = first.session_id, first.paths
        second_id = second.session_id
    repository.remove(first_id)
    assert not first_paths.root.exists()
    assert repository.read(second_id)
    assert legacy.read_text(encoding="utf-8") == "用户旧数据"


def test_control_directory_link_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / ".lancher"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(SessionRepositoryError, match="路径"):
            create(ProjectSessionRepository(tmp_path))
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_writer_rechecks_workspace_junction_before_append(tmp_path):
    if os.name != "nt":
        pytest.skip("此项验证 Windows junction，POSIX 链接由另一项覆盖。")
    repository = ProjectSessionRepository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with create(repository) as writer:
        writer.paths.workspace.rmdir()
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(writer.paths.workspace), str(outside)], capture_output=True)
        assert result.returncode == 0
        try:
            with pytest.raises(SessionRepositoryError, match="junction"):
                writer.append("state.changed", {"state": {}})
        finally:
            writer.paths.workspace.rmdir()


def test_one_writer_serializes_concurrent_threads(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        with ThreadPoolExecutor(max_workers=4) as executor:
            events = list(executor.map(
                lambda index: writer.append("custom.recorded", {"index": index}), range(20),
            ))
        assert sorted(event["seq"] for event in events) == list(range(2, 22))
        assert [event["seq"] for event in repository.read(writer.session_id)] == list(range(1, 22))


def test_active_writer_list_rebuild_does_not_write_without_lock(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        writer.append("message.created", {"id": "m1", "role": "user", "content": "正文"})
        writer.paths.metadata.unlink()
        assert repository.list_sessions()[0].message_count == 1
        assert not writer.paths.metadata.exists()
    assert repository.list_sessions()[0].message_count == 1
    assert writer.paths.metadata.exists()


def test_invalid_initial_payload_has_no_allocated_session(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    session_id = uuid4().hex
    with pytest.raises(SessionRepositoryError, match="元数据"):
        repository.create(session_id, "标题", initial_data(rules="bad"))
    assert not repository.paths(session_id).root.exists()


def test_invalid_event_payload_is_rejected_before_log_append(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        before = writer.paths.events.read_bytes()
        with pytest.raises(SessionRepositoryError, match="事件内容"):
            writer.append("permissions.changed", {"rules": "not a list"})
        with pytest.raises(SessionRepositoryError, match="事件内容"):
            writer.append("model.changed", {"model_ref": 123})
        assert writer.paths.events.read_bytes() == before


def test_checkpoint_validates_state_and_supports_middle_log_prefix(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        writer.append("state.changed", {"state": {"first": True}})
        writer.checkpoint({"cached": "at second event"})
        writer.append("state.changed", {"state": {"later": True}})
        result = repository.load_checkpoint(writer.session_id)
        assert result == {"last_seq": 2, "state": {"cached": "at second event"}}
        raw = json.loads(writer.paths.checkpoint.read_text(encoding="utf-8"))
        assert len(raw["events_sha256"]) == len(raw["state_sha256"]) == 64
        raw["state"]["cached"] = "伪造状态"
        writer.paths.checkpoint.write_text(json.dumps(raw), encoding="utf-8")
        assert repository.load_checkpoint(writer.session_id) is None


def test_checkpoint_detects_changed_log_prefix_and_obsolete_cache(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        writer.append("message.created", {"id": "u1", "role": "user", "content": "before"})
        writer.checkpoint({"messages": ["before"]})
        session_id, paths = writer.session_id, writer.paths
    checkpoint = paths.checkpoint.read_bytes()
    original_events = paths.events.read_bytes()
    lines = paths.events.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[1])
    event["data"]["content"] = "after"
    paths.events.write_text(lines[0] + "\n" + json.dumps(event) + "\n", encoding="utf-8")
    assert repository.load_checkpoint(session_id) is None
    paths.events.write_bytes(original_events)
    paths.checkpoint.write_bytes(checkpoint)
    assert repository.load_checkpoint(session_id) is not None
    old = json.loads(checkpoint)
    old.pop("events_sha256")
    paths.checkpoint.write_text(json.dumps(old), encoding="utf-8")
    assert repository.load_checkpoint(session_id) is None
    old = json.loads(checkpoint)
    old["last_seq"] = 99
    paths.checkpoint.write_text(json.dumps(old), encoding="utf-8")
    assert repository.load_checkpoint(session_id) is None


def test_checkpoint_never_masks_complete_log_corruption(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        writer.checkpoint({"valid": True})
        paths, session_id = writer.paths, writer.session_id
    with paths.events.open("ab") as stream:
        stream.write(b"complete bad line\n")
    with pytest.raises(SessionRepositoryError, match="第 2 行"):
        repository.load_checkpoint(session_id)


@pytest.mark.parametrize("field", ["events", "metadata", "checkpoint", "lock"])
def test_existing_control_hardlink_is_rejected_without_mutating_other_file(tmp_path, field):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        writer.checkpoint({"cached": True})
        session_id, paths = writer.session_id, writer.paths
    target = getattr(paths, field)
    before = target.read_bytes()
    alias = tmp_path / f"unrelated-{field}.bin"
    os.link(target, alias)
    for action in (repository.read, repository.open, repository.load_checkpoint):
        with pytest.raises(SessionRepositoryError, match="硬链接"):
            action(session_id)
    assert alias.read_bytes() == before
    assert target.read_bytes() == before
    alias.unlink()
    with repository.open(session_id) as resumed:
        resumed.append("custom.recorded", {"restored": True})


def test_active_writer_rechecks_real_control_hardlink_before_append(tmp_path):
    repository = ProjectSessionRepository(tmp_path)
    with create(repository) as writer:
        before = writer.paths.events.read_bytes()
        alias = tmp_path / "unrelated-log.bin"
        os.link(writer.paths.events, alias)
        try:
            with pytest.raises(SessionRepositoryError, match="硬链接"):
                writer.append("state.changed", {"state": {}})
            with pytest.raises(SessionRepositoryError, match="硬链接"):
                writer.checkpoint({"cached": True})
            assert alias.read_bytes() == before
        finally:
            alias.unlink()
        writer.append("state.changed", {"state": {}})


@pytest.mark.parametrize("failure", ["partial_write", "fsync"])
def test_failed_first_disk_write_removes_uncommitted_directory_and_allows_retry(tmp_path, monkeypatch, failure):
    repository = ProjectSessionRepository(tmp_path)
    session_id = uuid4().hex
    paths = repository.paths(session_id)
    written = []
    with monkeypatch.context() as patch:
        if failure == "fsync":
            def fail_fsync(fd):
                written.append(paths.events.read_bytes())
                raise OSError("首次日志同步失败")
            patch.setattr(os, "fsync", fail_fsync)
        else:
            original_open = Path.open
            class PartialWriteStream:
                def __init__(self, stream):
                    self.stream = stream

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def write(self, data):
                    part = data[:len(data) // 2]
                    self.stream.write(part)
                    self.stream.flush()
                    written.append(part)
                    raise OSError("首次日志写到一半失败")

            def open_with_partial_write(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if path == paths.events and args and args[0] == "ab":
                    return PartialWriteStream(stream)
                return stream
            patch.setattr(Path, "open", open_with_partial_write)
        with pytest.raises(SessionRepositoryError, match="首次日志"):
            repository.create(session_id, "首消息", initial_data())
    assert written and written[0]
    assert not paths.root.exists()
    assert repository.list_sessions() == []
    # 相同 UUID 能再创建，同时验证失败后 OS 锁已释放。
    with repository.create(session_id, "重试成功", initial_data()) as writer:
        assert writer.info.title == "重试成功"
        assert len(repository.read(session_id)) == 1


def test_failed_creation_is_quarantined_if_directory_removal_fails(tmp_path, monkeypatch):
    repository = ProjectSessionRepository(tmp_path)
    session_id = uuid4().hex
    paths = repository.paths(session_id)
    with monkeypatch.context() as patch:
        def fail_sync(fd):
            raise OSError("首次日志同步失败")
        def fail_removal(path):
            raise OSError("目录暂时无法删除")
        patch.setattr(os, "fsync", fail_sync)
        patch.setattr(storage.shutil, "rmtree", fail_removal)
        with pytest.raises(SessionRepositoryError, match="首次日志"):
            repository.create(session_id, "首消息", initial_data())
    assert not paths.root.exists()
    failed = list(repository.session_dir.glob(f".failed-{session_id}-*"))
    assert len(failed) == 1
    assert (failed[0] / "events.jsonl").is_file()
    assert repository.list_sessions() == []
    with repository.create(session_id, "重试成功", initial_data()):
        pass


def test_failed_writer_construction_cleans_allocated_directory(tmp_path, monkeypatch):
    repository = ProjectSessionRepository(tmp_path)
    session_id = uuid4().hex
    original_open = Path.open
    def fail_event_open(path, *args, **kwargs):
        if path.name == "events.jsonl" and args and args[0] == "ab":
            raise OSError("日志文件无法创建")
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_event_open)
        with pytest.raises(SessionRepositoryError, match="无法创建"):
            repository.create(session_id, "首消息", initial_data())
    assert repository.list_sessions() == []
    assert not repository.paths(session_id).root.exists()
    with repository.create(session_id, "重试", initial_data()):
        pass


def test_creation_failure_after_confirmed_commit_preserves_valid_session(tmp_path, monkeypatch):
    repository = ProjectSessionRepository(tmp_path)
    session_id = uuid4().hex
    original_append = storage.SessionWriter.append
    def commit_then_fail(writer, *args, **kwargs):
        original_append(writer, *args, **kwargs)
        raise SessionRepositoryError("创建事件已提交后的故障")
    with monkeypatch.context() as patch:
        patch.setattr(storage.SessionWriter, "append", commit_then_fail)
        with pytest.raises(SessionRepositoryError, match="已提交后的故障"):
            repository.create(session_id, "已经持久化", initial_data())
    assert repository.paths(session_id).root.is_dir()
    assert repository.read(session_id)[0]["data"]["title"] == "已经持久化"
    assert repository.list_sessions()[0].session_id == session_id
    with repository.open(session_id) as writer:
        assert writer.append("state.changed", {"state": {}})["seq"] == 2
