from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from lancher_code.contracts.control import CancellationToken
from lancher_code.filesystem.deletion import execute_delete, plan_delete
from lancher_code.filesystem.access import PathWriteDeniedError
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.delete_file import DeleteFileTool
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.file_state_cache import FileStateCache


def _context(root: Path, **kwargs: object) -> ToolContext:
    return ToolContext(cwd=root, timeout_seconds=1, file_state_cache=FileStateCache(), **kwargs)


def _run(arguments: dict[str, object], context: ToolContext):
    return asyncio.run(DeleteFileTool().execute(arguments, context))


def test_delete_file_is_registered() -> None:
    definition = create_default_tool_registry().get("delete_file").definition
    assert definition.category == "write"
    assert definition.permission.display_name == "DeleteFile"


def test_delete_single_file_clears_old_read_state(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")
    context = _context(tmp_path)
    context.file_state_cache.record_read(path, "old", mtime_ns=path.stat().st_mtime_ns, is_complete=True)
    result = _run({"path": "example.txt"}, context)
    assert not result.is_error
    assert not path.exists()
    assert result.metadata["deleted_count"] == 1
    path.write_text("new", encoding="utf-8")
    assert context.file_state_cache.get(path) is None


def test_directory_requires_explicit_recursive(tmp_path: Path) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "example.txt").write_text("old", encoding="utf-8")
    result = _run({"path": "nested"}, _context(tmp_path))
    assert result.error_code == "recursive_required"
    assert directory.exists()


def test_cached_manifest_cannot_delete_another_call_target(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    context = _context(tmp_path)
    context.delete_plan = plan_delete({"path": "a.txt"}, context)
    result = _run({"path": "b.txt"}, context)
    assert result.error_code == "delete_target_changed"
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").exists()


def test_recursive_delete_manifest_contains_all_items(tmp_path: Path) -> None:
    directory = tmp_path / "nested"
    (directory / "sub").mkdir(parents=True)
    (directory / "a.txt").write_text("a", encoding="utf-8")
    (directory / "sub" / "b.txt").write_text("bb", encoding="utf-8")
    context = _context(tmp_path)
    plan = plan_delete({"path": "nested", "recursive": True}, context)
    assert plan.is_directory
    assert len(plan.entries) == 4
    assert plan.total_bytes == 3
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert not result.is_error
    assert result.metadata["deleted_count"] == 4
    assert not directory.exists()


def test_resource_claims_do_not_scan_recursive_manifest(tmp_path: Path, monkeypatch) -> None:
    from lancher_code.tools.builtin import delete_file

    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "example.txt").write_text("content", encoding="utf-8")

    def refuse_to_scan(*args, **kwargs):
        raise AssertionError("资源声明不应生成完整删除清单。")

    monkeypatch.setattr(delete_file, "plan_delete", refuse_to_scan)
    claims = DeleteFileTool().resource_claims({"path": "nested", "recursive": True}, _context(tmp_path))
    assert len(claims) == 1
    assert claims[0].kind == "path"
    assert claims[0].recursive is True
    assert claims[0].mode == "exclusive"


@pytest.mark.parametrize("raw_path", [".", "..", ".lancher/sessions", ".lancher/sessions/" + "a" * 32 + "/events.jsonl"])
def test_protected_and_outside_paths_are_rejected(tmp_path: Path, raw_path: str) -> None:
    result = _run({"path": raw_path, "recursive": True}, _context(tmp_path))
    assert result.is_error
    assert result.error_code in {"delete_root_protected", "path_outside_project", "git_metadata_protected", "session_records_protected"}


def test_recursive_parent_containing_git_metadata_can_be_deleted(tmp_path: Path) -> None:
    directory = tmp_path / "subrepo"
    (directory / ".git").mkdir(parents=True)
    (directory / "a.txt").write_text("a", encoding="utf-8")
    result = _run({"path": "subrepo", "recursive": True}, _context(tmp_path))
    assert not result.is_error
    assert not directory.exists()


def test_recursive_session_parent_is_rejected_without_deletion(tmp_path: Path) -> None:
    directory = tmp_path / ".lancher" / "sessions" / ("a" * 32)
    (directory / "workspace").mkdir(parents=True)
    (directory / "events.jsonl").write_text("event", encoding="utf-8")
    result = _run({"path": ".lancher", "recursive": True}, _context(tmp_path))
    assert result.error_code == "session_records_protected"
    assert (directory / "events.jsonl").exists()


def test_plan_changed_by_new_directory_item_prevents_all_deletions(tmp_path: Path) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    context = _context(tmp_path)
    plan = plan_delete({"path": "nested", "recursive": True}, context)
    (directory / "b.txt").write_text("b", encoding="utf-8")
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert result.error_code == "delete_target_changed"
    assert (directory / "a.txt").exists()
    assert (directory / "b.txt").exists()


def test_changed_file_after_plan_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")
    context = _context(tmp_path)
    plan = plan_delete({"path": "example.txt"}, context)
    path.write_text("changed", encoding="utf-8")
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert result.error_code == "delete_target_changed"
    assert path.read_text(encoding="utf-8") == "changed"


def test_replaced_file_with_identical_size_and_mtime_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")
    context = _context(tmp_path)
    plan = plan_delete({"path": "example.txt"}, context)
    old_mtime = path.stat().st_mtime_ns
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("new", encoding="utf-8")
    os.utime(replacement, ns=(old_mtime, old_mtime))
    os.replace(replacement, path)
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert result.error_code == "delete_target_changed"
    assert path.read_text(encoding="utf-8") == "new"


def test_rebuilt_file_cannot_reuse_old_read_authorization(tmp_path: Path) -> None:
    from lancher_code.tools.builtin.write_file import WriteFileTool

    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")
    context = _context(tmp_path)
    context.file_state_cache.record_read(path, "old", mtime_ns=path.stat().st_mtime_ns, is_complete=True)
    assert not _run({"path": "example.txt"}, context).is_error
    path.write_text("new", encoding="utf-8")
    result = asyncio.run(WriteFileTool().execute({"path": "example.txt", "content": "overwrite"}, context))
    assert result.error_code == "stale_file_state"
    assert path.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("phase", ["discuss", "plan"])
def test_early_phase_delete_is_readonly_even_in_current_workspace(tmp_path: Path, phase: str) -> None:
    session_id = "a" * 32
    session_root = tmp_path / ".lancher" / "sessions" / session_id
    workspace = session_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "scratch.txt").write_text("scratch", encoding="utf-8")
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    context = _context(tmp_path, work_phase=phase, session_id=session_id,
                       session_root=session_root, session_workspace=workspace)
    assert _run({"path": "source.txt"}, context).error_code == "phase_disallowed"
    result = _run({"path": str(workspace / "scratch.txt")}, context)
    assert result.error_code == "phase_disallowed"
    assert (workspace / "scratch.txt").exists()
    assert (tmp_path / "source.txt").exists()


def test_cancelled_delete_does_not_change_file(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("old", encoding="utf-8")
    token = CancellationToken()
    context = _context(tmp_path, cancellation_token=token)
    plan = plan_delete({"path": "example.txt"}, context)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute_delete(plan, context))
    assert path.exists()


def test_cancel_mid_directory_reports_actual_partial_result(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    (directory / "b.txt").write_text("b", encoding="utf-8")
    token = CancellationToken()
    context = _context(tmp_path, cancellation_token=token)
    plan = plan_delete({"path": "nested", "recursive": True}, context)
    original = Path.unlink

    def cancel_after_unlink(path, *args, **kwargs):
        original(path, *args, **kwargs)
        token.cancel()

    monkeypatch.setattr(Path, "unlink", cancel_after_unlink)
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert result.error_code == "delete_interrupted"
    assert result.metadata["partial"] is True
    assert result.metadata["deleted_count"] == 1
    assert len(list(directory.iterdir())) == 1


def test_invalid_recursive_argument_is_rejected(tmp_path: Path) -> None:
    result = _run({"path": "example.txt", "recursive": "yes"}, _context(tmp_path))
    assert result.error_code == "invalid_arguments"


def test_authorized_delete_outside_via_parent_link_removes_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    path = outside / "example.txt"
    path.write_text("old", encoding="utf-8")
    _directory_link(tmp_path / "link", outside)
    result = _run({"path": "link/example.txt"}, _context(tmp_path))
    assert not result.is_error
    assert not path.exists()


def test_recursive_delete_unlinks_junction_without_following(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    path = outside / "example.txt"
    path.write_text("old", encoding="utf-8")
    directory = tmp_path / "nested"
    directory.mkdir()
    _directory_link(directory / "link", outside)
    result = _run({"path": "nested", "recursive": True}, _context(tmp_path))
    assert not result.is_error
    assert not directory.exists()
    assert path.read_text(encoding="utf-8") == "old"


def test_delete_link_itself_preserves_external_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    path = outside / "example.txt"
    path.write_text("old", encoding="utf-8")
    _directory_link(tmp_path / "link", outside)
    result = _run({"path": "link"}, _context(tmp_path))
    assert not result.is_error
    assert not (tmp_path / "link").exists()
    assert path.exists()


def test_delete_external_link_claims_parent_instead_of_external_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    _directory_link(tmp_path / "link", outside)
    claims = DeleteFileTool().resource_claims({"path": "link"}, _context(tmp_path))
    assert len(claims) == 1
    assert claims[0].kind == "path"
    assert claims[0].recursive is True
    assert claims[0].key != str(outside)


def test_directory_change_during_scanning_prevents_manifest(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    original = os.scandir

    class ChangedScandir:
        def __init__(self, path):
            self.path = Path(path)
            self.iterator = original(path)

        def __enter__(self):
            return self.iterator.__enter__()

        def __exit__(self, *args):
            result = self.iterator.__exit__(*args)
            if self.path == directory:
                (directory / "late.txt").write_text("late", encoding="utf-8")
            return result

    monkeypatch.setattr(os, "scandir", ChangedScandir)
    with pytest.raises(PathWriteDeniedError) as error:
        plan_delete({"path": "nested", "recursive": True}, _context(tmp_path))
    assert error.value.reason_code == "delete_target_changed"
    assert (directory / "a.txt").exists()


def test_directory_replaced_by_external_link_after_plan_is_preserved(tmp_path: Path) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    (outside / "a.txt").write_text("outside", encoding="utf-8")
    context = _context(tmp_path)
    plan = plan_delete({"path": "nested", "recursive": True}, context)
    saved = tmp_path / "saved"
    directory.rename(saved)
    _directory_link(directory, outside)
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert result.error_code == "delete_target_changed"
    assert (saved / "a.txt").exists()
    assert (outside / "a.txt").read_text(encoding="utf-8") == "outside"


def test_parent_redirected_mid_delete_stops_before_touching_external_file(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "a.txt").write_text("a", encoding="utf-8")
    (directory / "b.txt").write_text("b", encoding="utf-8")
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir()
    (outside / "a.txt").write_text("outside", encoding="utf-8")
    context = _context(tmp_path)
    plan = plan_delete({"path": "nested", "recursive": True}, context)
    original = Path.unlink

    def redirect_after_unlink(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path == directory / "b.txt":
            directory.rename(tmp_path / "saved")
            _directory_link(directory, outside)

    monkeypatch.setattr(Path, "unlink", redirect_after_unlink)
    result = asyncio.run(DeleteFileTool().execute_plan(plan, context))
    assert result.error_code == "delete_target_changed"
    assert result.metadata["partial"] is True
    assert result.metadata["deleted_count"] == 1
    assert (outside / "a.txt").read_text(encoding="utf-8") == "outside"
    assert (tmp_path / "saved" / "a.txt").exists()


def _directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if completed.returncode:
            pytest.skip("当前环境不能创建 Windows junction。")
    else:
        link.symlink_to(target, target_is_directory=True)
