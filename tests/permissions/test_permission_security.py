from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.models import DirectoryGrant, PermissionResolution
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.file_state_cache import FileStateCache


def _call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(0, "security-call", name, arguments, "{}")


def _context(project: Path, *, phase: str = "execute", access: str = "project") -> ToolContext:
    return ToolContext(cwd=project, project_root=project, timeout_seconds=2, work_phase=phase,
                       execution_access=access, file_state_cache=FileStateCache())


def _check(project: Path, name: str, args: dict, *, access: str = "project", storage=None):
    return PermissionEngine(storage).evaluate(call=_call(name, args), tool=create_default_tool_registry().get(name).definition,
                                              context=_context(project, access=access))


def _shell_delete(path: Path, *, recursive: bool = True) -> str:
    quoted = f'"{path.as_posix()}"'
    if os.name == "nt":
        return f"Remove-Item -LiteralPath {quoted}" + (" -Recurse" if recursive else "")
    return f"rm {'-r ' if recursive else ''}{quoted}"


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"环境不允许创建符号链接：{exc}")


def _directory_link(link: Path, target: Path) -> None:
    """Windows junction 不需要符号链接特权，必须覆盖真实的目录重定向。"""
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    quoted_link, quoted_target = str(link).replace("'", "''"), str(target).replace("'", "''")
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command",
                             f"New-Item -ItemType Junction -Path '{quoted_link}' -Target '{quoted_target}' -ErrorAction Stop | Out-Null"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert link.is_junction()


@pytest.mark.parametrize("access", ["project", "full"])
def test_project_recursive_delete_is_automatic(tmp_path: Path, access: str) -> None:
    target = tmp_path / "temporary"
    target.mkdir()
    (target / "data.txt").write_text("data", encoding="utf-8")
    check = _check(tmp_path, "delete_file", {"path": str(target), "recursive": True}, access=access)
    assert check.decision == "allow" and check.request is None
    assert check.metadata["delete_signature"]


@pytest.mark.parametrize("access,decision", [("project", "ask"), ("full", "allow")])
def test_external_recursive_delete_follows_folder_scope(tmp_path: Path, access: str, decision: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    (outside / "data.txt").write_text("data", encoding="utf-8")
    check = _check(project, "delete_file", {"path": str(outside), "recursive": True}, access=access)
    assert check.decision == decision
    if check.request is not None:
        assert check.request.directory_grants == [DirectoryGrant(str(outside), "write")]
        assert "data.txt" in check.request.details


@pytest.mark.parametrize("access", ["project", "full"])
@pytest.mark.parametrize("relative", [".", ".lancher/sessions/" + "a" * 32 + "/events.jsonl"])
def test_project_root_and_session_records_are_protected_in_both_modes(tmp_path: Path, access: str, relative: str) -> None:
    record = tmp_path / ".lancher" / "sessions" / ("a" * 32) / "events.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text("record", encoding="utf-8")
    check = _check(tmp_path, "delete_file", {"path": relative, "recursive": True}, access=access)
    assert check.decision == "deny"
    assert record.read_text(encoding="utf-8") == "record"


@pytest.mark.parametrize("access", ["project", "full"])
def test_git_metadata_regular_delete_is_allowed(tmp_path: Path, access: str) -> None:
    target = tmp_path / ".git" / "temporary"
    target.parent.mkdir()
    target.write_text("data", encoding="utf-8")
    assert _check(tmp_path, "delete_file", {"path": str(target)}, access=access).decision == "allow"
    assert _check(tmp_path, "run_command", {"command": _shell_delete(target, recursive=False)}, access=access).decision == "allow"


@pytest.mark.parametrize("access", ["project", "full"])
def test_ordinary_git_reset_clean_checkout_restore_can_execute(tmp_path: Path, access: str) -> None:
    for command in ("git reset --hard", "git clean -fd", "git checkout -- data.txt", "git restore --source HEAD data.txt"):
        assert _check(tmp_path, "run_command", {"command": command}, access=access).decision == "allow"


@pytest.mark.parametrize("access", ["project", "full"])
@pytest.mark.parametrize("command", ["diskpart", "mkfs.ext4 /dev/sda"])
def test_disk_destruction_still_hard_denied(tmp_path: Path, access: str, command: str) -> None:
    check = _check(tmp_path, "run_command", {"command": command}, access=access)
    assert check.decision == "deny" and check.reason_code == "permission_blacklist_denied"


@pytest.mark.parametrize("access", ["project", "full"])
def test_whole_project_wildcard_delete_hard_denied(tmp_path: Path, access: str) -> None:
    command = (f'Remove-Item "{tmp_path.as_posix()}/*" -Recurse' if os.name == "nt"
               else f'rm -rf "{tmp_path.as_posix()}"/*')
    check = _check(tmp_path, "run_command", {"command": command}, access=access)
    assert check.decision == "deny" and check.reason_code == "delete_root_protected"


def test_extension_glob_delete_is_not_mistaken_for_whole_project(tmp_path: Path) -> None:
    command = 'Remove-Item "*.txt"' if os.name == "nt" else 'rm *.txt'
    assert _check(tmp_path, "run_command", {"command": command}, access="full").decision == "allow"


def test_empty_directory_shell_remove_does_not_require_native_recursive_argument(tmp_path: Path) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    command = f'Remove-Item -LiteralPath "{target.as_posix()}"' if os.name == "nt" else f'rmdir "{target.as_posix()}"'
    assert _check(tmp_path, "run_command", {"command": command}).decision == "allow"


@pytest.mark.parametrize("access", ["project", "full"])
def test_shell_known_write_to_session_control_records_cannot_bypass_protection(tmp_path: Path, access: str) -> None:
    record = tmp_path / ".lancher" / "sessions" / ("a" * 32) / "events.jsonl"
    command = f'Write-Output hello > "{record.as_posix()}"' if os.name == "nt" else f'printf hello > "{record.as_posix()}"'
    check = _check(tmp_path, "run_command", {"command": command}, access=access)
    assert check.decision == "deny" and check.reason_code == "session_records_protected"


def test_long_command_detects_external_access_beyond_ui_preview(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    quoted = "ordinary " * 700
    command = (f'Write-Output "{quoted}"; Get-Content "{outside.as_posix()}/data.txt"' if os.name == "nt"
               else f'printf "{quoted}"; cat "{outside.as_posix()}/data.txt"')
    check = _check(project, "run_command", {"command": command})
    assert check.decision == "ask" and check.request is not None
    assert check.request.command == command
    assert check.request.directory_grants == [DirectoryGrant(str(outside), "read")]


def test_file_symlink_to_external_requires_real_target_read_grant(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    link = project / "link.txt"
    _symlink(link, target)
    check = _check(project, "read_file", {"path": str(link)})
    assert check.decision == "ask" and check.request is not None
    assert check.request.directory_grants == [DirectoryGrant(str(outside), "read")]
    assert str(target) in check.request.details


def test_delete_project_symlink_only_deletes_link_without_external_approval(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "data.txt"
    target.write_text("data", encoding="utf-8")
    link = project / "link.txt"
    _symlink(link, target)
    check = _check(project, "delete_file", {"path": str(link)})
    assert check.decision == "allow"
    assert check.metadata["path_accesses"][0]["path"] == str(link)


def test_directory_grant_restoration_does_not_follow_redirected_grant_root(tmp_path: Path) -> None:
    project, authorized, other = tmp_path / "project", tmp_path / "authorized", tmp_path / "other"
    project.mkdir(); authorized.mkdir(); other.mkdir()
    grant = DirectoryGrant(str(authorized), "write")
    authorized.rmdir()
    _symlink(authorized, other, directory=True)
    storage = PermissionStorage()
    storage.replace_session_grants([grant])
    check = _check(project, "write_file", {"path": str(authorized / "file.txt"), "content": "data"}, storage=storage)
    assert check.decision == "ask"
    assert storage.session_grants == [grant]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["allow_once", "allow_session"])
async def test_approved_external_write_executes_and_only_session_option_retains_grant(tmp_path: Path, outcome: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    storage = PermissionStorage()
    requests = []
    async def approve(request):
        requests.append(request)
        return PermissionResolution(request.request_id, outcome)
    result = (await ToolExecutor(create_default_tool_registry(), cwd=project, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("write_file", {"path": str(outside / "created.txt"), "content": "created"})], permission_resolver=approve))[0]
    assert not result.is_error and len(requests) == 1
    assert (outside / "created.txt").read_text(encoding="utf-8") == "created"
    assert storage.session_grants == ([DirectoryGrant(str(outside), "write")] if outcome == "allow_session" else [])


@pytest.mark.asyncio
async def test_project_recursive_delete_executes_without_resolver(tmp_path: Path) -> None:
    target = tmp_path / "temporary"
    target.mkdir()
    (target / "data.txt").write_text("data", encoding="utf-8")
    result = (await ToolExecutor(create_default_tool_registry(), cwd=tmp_path).execute_calls(
        [_call("delete_file", {"path": str(target), "recursive": True})]))[0]
    assert not result.is_error and not target.exists()


@pytest.mark.asyncio
async def test_external_delete_changed_during_approval_stops_without_storing_grant(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "temporary.txt"
    target.write_text("old", encoding="utf-8")
    storage = PermissionStorage()
    async def approve(request):
        target.write_text("replacement version", encoding="utf-8")
        return PermissionResolution(request.request_id, "allow_session")
    result = (await ToolExecutor(create_default_tool_registry(), cwd=project, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("delete_file", {"path": str(target)})], permission_resolver=approve))[0]
    assert result.is_error and result.error_code in {"permission_target_changed", "delete_target_changed", "resource_target_changed"}
    assert target.read_text(encoding="utf-8") == "replacement version"
    assert storage.session_grants == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "supersede"])
async def test_external_approval_interrupted_keeps_target_and_does_not_grant(tmp_path: Path, change: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    target = outside / "temporary.txt"
    target.write_text("keep", encoding="utf-8")
    storage, token = PermissionStorage(), CancellationToken()
    superseded = False
    async def approve(request):
        nonlocal superseded
        if change == "cancel":
            token.cancel()
        else:
            superseded = True
        return PermissionResolution(request.request_id, "allow_session")
    operation = ToolExecutor(create_default_tool_registry(), cwd=project, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("delete_file", {"path": str(target)})], permission_resolver=approve,
        cancellation_token=token, should_interrupt=lambda: superseded)
    if change == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        assert (await operation)[0].error_code == "steering_superseded"
    assert target.read_text(encoding="utf-8") == "keep" and storage.session_grants == []


@pytest.mark.asyncio
async def test_approval_callback_cannot_redirect_link_to_a_new_outside_target(tmp_path: Path) -> None:
    project, first, second = tmp_path / "project", tmp_path / "first", tmp_path / "second"
    project.mkdir(); first.mkdir(); second.mkdir()
    (first / "file.txt").write_text("first", encoding="utf-8")
    (second / "file.txt").write_text("second", encoding="utf-8")
    link = project / "link.txt"
    _symlink(link, first / "file.txt")
    storage = PermissionStorage()
    async def approve(request):
        link.unlink()
        link.symlink_to(second / "file.txt")
        return PermissionResolution(request.request_id, "allow_session")
    result = (await ToolExecutor(create_default_tool_registry(), cwd=project, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("read_file", {"path": str(link)})], permission_resolver=approve))[0]
    assert result.is_error and result.error_code in {"permission_target_changed", "resource_target_changed"}
    assert storage.session_grants == []


@pytest.mark.asyncio
async def test_project_write_argument_changed_after_started_callback_is_stopped(tmp_path: Path) -> None:
    async def change_target(call):
        call.arguments["path"] = "replacement.txt"
    result = (await ToolExecutor(create_default_tool_registry(), cwd=tmp_path).execute_calls(
        [_call("write_file", {"path": "original.txt", "content": "must not write"})], on_call_started=change_target))[0]
    assert result.error_code in {"resource_target_changed", "permission_target_changed"}
    assert not (tmp_path / "original.txt").exists() and not (tmp_path / "replacement.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["token", "timeout"])
async def test_cancelled_tool_cleanup_result_preserves_partial_effects(tmp_path: Path, stop: str) -> None:
    token = CancellationToken()
    context = _context(tmp_path)
    context.cancellation_token = token
    expected = ToolExecutionResult("partial-call", "delete_file", is_error=True, error_code="delete_interrupted",
        metadata={"partial": True, "deleted_count": 1, "automatic_retry": False})
    async def cleanup_after_partial_commit():
        if stop == "token":
            token.cancel()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return expected
    actual = await ToolExecutor._await_cancelable(cleanup_after_partial_commit(), context, timeout=0.01 if stop == "timeout" else None)
    assert actual is expected and actual.metadata["partial"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["token", "task"])
async def test_recursive_delete_cancel_reports_partial_paths_through_executor(tmp_path: Path, monkeypatch, stop: str) -> None:
    from lancher_code.filesystem import deletion
    target = tmp_path / "temporary"
    target.mkdir()
    (target / "first.txt").write_text("first", encoding="utf-8")
    (target / "second.txt").write_text("second", encoding="utf-8")
    token, reported = CancellationToken(), []
    original_unlink, original_sleep = Path.unlink, asyncio.sleep
    parent_task = None
    def cancel_after_unlink(path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if path.parent == target:
            token.cancel()
            if stop == "task":
                parent_task.cancel()
    async def pause_at_next_commit(delay, *args, **kwargs):
        if token.is_cancelled:
            await asyncio.Event().wait()
        else:
            await original_sleep(delay, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", cancel_after_unlink)
    monkeypatch.setattr(deletion.asyncio, "sleep", pause_at_next_commit)
    async def report(result):
        reported.append(result)
    parent_task = asyncio.create_task(ToolExecutor(create_default_tool_registry(), cwd=tmp_path).execute_calls(
        [_call("delete_file", {"path": str(target), "recursive": True})], cancellation_token=token, on_result=report))
    if stop == "task":
        with pytest.raises(asyncio.CancelledError):
            await parent_task
        results = reported
    else:
        results = await parent_task
    result = results[0]
    assert result.error_code == "delete_interrupted" and result.metadata["partial"] is True
    assert result.metadata["deleted_count"] == 1 and result.metadata["automatic_retry"] is False
    assert len(list(target.iterdir())) == 1 and reported == [result]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["glob", "grep"])
async def test_project_search_skips_outside_junction_but_direct_link_path_requests_permission(tmp_path: Path, name: str) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    (project / "local.txt").write_text("local data", encoding="utf-8")
    secret = outside / "secret.txt"
    secret.write_text("private-secret-marker never-on-project", encoding="utf-8")
    link = project / "linked-outside"
    _directory_link(link, outside)
    args = {"pattern": "private-secret-marker" if name == "grep" else "**/*.txt"}
    result = (await ToolExecutor(create_default_tool_registry(), cwd=project).execute_calls([_call(name, args)]))[0]
    assert not result.is_error
    assert "private-secret-marker never-on-project" not in result.content and "secret.txt" not in result.content
    if name == "grep":
        assert result.metadata["matches"] == []
    direct = _check(project, name, {**args, "path": str(link)})
    assert direct.decision == "ask" and direct.request is not None
    assert direct.request.directory_grants == [DirectoryGrant(str(outside), "read")]


@pytest.mark.asyncio
async def test_directory_junction_retargeted_during_approval_stops_without_grant(tmp_path: Path) -> None:
    project, first, second = tmp_path / "project", tmp_path / "first", tmp_path / "second"
    project.mkdir(); first.mkdir(); second.mkdir()
    (first / "data.txt").write_text("first", encoding="utf-8")
    (second / "data.txt").write_text("second", encoding="utf-8")
    link = project / "linked"
    _directory_link(link, first)
    storage = PermissionStorage()
    async def approve(request):
        link.rmdir() if os.name == "nt" else link.unlink()
        _directory_link(link, second)
        return PermissionResolution(request.request_id, "allow_session")
    result = (await ToolExecutor(create_default_tool_registry(), cwd=project, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("read_file", {"path": str(link / "data.txt")})], permission_resolver=approve))[0]
    assert result.is_error and result.error_code in {"permission_target_changed", "resource_target_changed"}
    assert storage.session_grants == []
    assert (first / "data.txt").read_text(encoding="utf-8") == "first"
    assert (second / "data.txt").read_text(encoding="utf-8") == "second"


def test_recursive_shell_delete_junction_requires_outside_target_scope(tmp_path: Path) -> None:
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir(); outside.mkdir()
    (outside / "data.txt").write_text("data", encoding="utf-8")
    link = project / "linked"
    _directory_link(link, outside)
    structured = _check(project, "delete_file", {"path": str(link), "recursive": True})
    assert structured.decision == "allow"
    shell = _check(project, "run_command", {"command": _shell_delete(link)})
    assert shell.decision == "ask" and shell.request is not None
    assert shell.request.directory_grants == [DirectoryGrant(str(outside), "write")]
    assert any(item["operation"] == "delete_link_target" for item in shell.metadata["path_accesses"])


@pytest.mark.parametrize("pattern", ["?*", "*?", "*?*", "?**"])
@pytest.mark.parametrize("protected", ["project", "volume", "home"])
@pytest.mark.parametrize("access", ["project", "full"])
def test_equivalent_all_name_patterns_cannot_recursively_delete_protected_roots(tmp_path: Path, pattern: str, protected: str, access: str) -> None:
    target = {"project": tmp_path, "volume": Path(tmp_path.anchor), "home": Path.home().resolve()}[protected]
    wildcard = target / pattern
    command = f'Remove-Item "{wildcard.as_posix()}" -Recurse' if os.name == "nt" else f'rm -r "{wildcard.as_posix()}"'
    check = _check(tmp_path, "run_command", {"command": command}, access=access)
    assert check.decision == "deny"
    assert check.reason_code in {"delete_root_protected", "delete_volume_root_protected", "delete_home_protected"}


@pytest.mark.parametrize("pattern", ["*.txt", "?abc*"])
@pytest.mark.parametrize("access", ["project", "full"])
def test_targeted_name_patterns_are_allowed_in_project_root(tmp_path: Path, pattern: str, access: str) -> None:
    wildcard = tmp_path / pattern
    command = f'Remove-Item "{wildcard.as_posix()}" -Recurse' if os.name == "nt" else f'rm -r "{wildcard.as_posix()}"'
    assert _check(tmp_path, "run_command", {"command": command}, access=access).decision == "allow"


@pytest.mark.parametrize("pattern", ["?*", "*?", "*?*", "?**"])
@pytest.mark.parametrize("access", ["project", "full"])
def test_equivalent_all_name_patterns_are_allowed_for_normal_subdirectories(tmp_path: Path, pattern: str, access: str) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    wildcard = ordinary / pattern
    command = f'Remove-Item "{wildcard.as_posix()}" -Recurse' if os.name == "nt" else f'rm -r "{wildcard.as_posix()}"'
    assert _check(tmp_path, "run_command", {"command": command}, access=access).decision == "allow"
