from __future__ import annotations

from pathlib import Path

import pytest

from lancher_code.filesystem.deletion import protected_delete_reason
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.models import DirectoryGrant, PermissionResolution
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.contracts.tools import ToolCall
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.executor import ToolExecutor


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(0, "access-call", name, arguments, "{}")


def folders(tmp_path: Path) -> tuple[Path, Path]:
    project, external = tmp_path / "project", tmp_path / "external"
    project.mkdir()
    external.mkdir()
    return project, external


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("read_file", {}), ("glob", {"pattern": "*.txt"}), ("grep", {"pattern": "needle"}),
])
async def test_project_external_reads_wait_for_approval_then_reach_real_target(tmp_path, name, arguments):
    project, external = folders(tmp_path)
    file = external / "note.txt"
    file.write_text("needle", encoding="utf-8")
    requests = []

    async def approve(request):
        requests.append(request)
        return PermissionResolution(request.request_id, "allow_once")

    executor = ToolExecutor(create_default_tool_registry(), cwd=project)
    target = file if name == "read_file" else external
    result = (await executor.execute_calls([call(name, path=str(target), **arguments)],
                                          permission_resolver=approve))[0]
    assert not result.is_error, result.content
    assert len(requests) == 1
    assert requests[0].directory_grants == [DirectoryGrant(str(external), "read")]
    assert "needle" in result.content if name != "glob" else "note.txt" in result.content


@pytest.mark.asyncio
async def test_project_external_write_requires_approval_and_full_does_not(tmp_path):
    project, external = folders(tmp_path)
    executor = ToolExecutor(create_default_tool_registry(), cwd=project)
    blocked = (await executor.execute_calls([call("write_file", path=str(external / "blocked.txt"), content="x")]))[0]
    assert blocked.error_code == "permission_confirmation_unavailable"
    assert not (external / "blocked.txt").exists()
    full = (await executor.execute_calls([call("write_file", path=str(external / "full.txt"), content="ok")],
                                        execution_access="full"))[0]
    assert not full.is_error
    assert (external / "full.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_successful_external_write_saves_directory_grant_for_next_call(tmp_path):
    project, external = folders(tmp_path)
    storage = PermissionStorage()
    executor = ToolExecutor(create_default_tool_registry(), cwd=project, permission_engine=PermissionEngine(storage))
    requests = []

    async def approve(request):
        requests.append(request)
        return PermissionResolution(request.request_id, "allow_session")

    for name in ("first.txt", "second.txt"):
        result = (await executor.execute_calls([call("write_file", path=str(external / name), content="ok")],
                                              permission_resolver=approve))[0]
        assert not result.is_error
    assert len(requests) == 1
    assert storage.session_grants == [DirectoryGrant(str(external), "write")]


@pytest.mark.asyncio
async def test_failed_external_write_does_not_save_directory_grant(tmp_path):
    project, external = folders(tmp_path)
    existing = external / "existing.txt"
    existing.write_text("original", encoding="utf-8")
    storage = PermissionStorage()

    async def approve(request):
        return PermissionResolution(request.request_id, "allow_session")

    result = (await ToolExecutor(create_default_tool_registry(), cwd=project,
              permission_engine=PermissionEngine(storage)).execute_calls([
                  call("write_file", path=str(existing), content="blind overwrite")], permission_resolver=approve))[0]
    assert result.error_code == "stale_file_state"
    assert existing.read_text(encoding="utf-8") == "original"
    assert storage.session_grants == []


@pytest.mark.asyncio
async def test_resolver_cannot_expand_the_frozen_directory_grant(tmp_path):
    project, external = folders(tmp_path)
    storage = PermissionStorage()

    async def approve(request):
        request.directory_grants[:] = [DirectoryGrant(str(Path(external.anchor)), "write")]
        return PermissionResolution(request.request_id, "allow_session")

    result = (await ToolExecutor(create_default_tool_registry(), cwd=project,
              permission_engine=PermissionEngine(storage)).execute_calls([
                  call("write_file", path=str(external / "note.txt"), content="ok")], permission_resolver=approve))[0]
    assert not result.is_error
    assert storage.session_grants == [DirectoryGrant(str(external), "write")]


@pytest.mark.asyncio
async def test_callback_cannot_change_once_approval_into_session_approval(tmp_path):
    project, external = folders(tmp_path)
    storage = PermissionStorage()
    returned = []

    async def approve(request):
        resolution = PermissionResolution(request.request_id, "allow_once")
        returned.append(resolution)
        return resolution

    async def mutate_after_approval(invocation):
        returned[0].outcome = "allow_session"

    result = (await ToolExecutor(create_default_tool_registry(), cwd=project,
              permission_engine=PermissionEngine(storage)).execute_calls([
                  call("write_file", path=str(external / "note.txt"), content="ok")],
                  permission_resolver=approve, on_call_started=mutate_after_approval))[0]
    assert not result.is_error
    assert storage.session_grants == []


@pytest.mark.asyncio
async def test_project_directory_deletion_is_automatic_and_external_deletion_is_approved(tmp_path):
    project, external = folders(tmp_path)
    internal = project / "generated"
    internal.mkdir()
    (internal / "file.txt").write_text("generated", encoding="utf-8")
    (external / "file.txt").write_text("generated", encoding="utf-8")
    executor = ToolExecutor(create_default_tool_registry(), cwd=project)
    result = (await executor.execute_calls([call("delete_file", path=str(internal), recursive=True)]))[0]
    assert not result.is_error
    assert not internal.exists()
    requests = []

    async def approve(request):
        requests.append(request)
        return PermissionResolution(request.request_id, "allow_once")

    result = (await executor.execute_calls([call("delete_file", path=str(external), recursive=True)],
                                          permission_resolver=approve))[0]
    assert not result.is_error
    assert not external.exists()
    assert len(requests) == 1
    assert str(external / "file.txt") in requests[0].file_paths


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discuss", "plan"])
@pytest.mark.parametrize("access", ["project", "full"])
async def test_readonly_phase_rejects_normal_workspace_write_and_delete(tmp_path, phase, access):
    project, _ = folders(tmp_path)
    session_id = "a" * 32
    session_root = project / ".lancher" / "sessions" / session_id
    workspace = session_root / "workspace"
    workspace.mkdir(parents=True)
    file = workspace / "note.txt"
    file.write_text("original", encoding="utf-8")
    executor = ToolExecutor(create_default_tool_registry(), cwd=project)
    for invocation in (call("write_file", path=str(file), content="changed"), call("delete_file", path=str(file))):
        result = (await executor.execute_calls([invocation], work_phase=phase, execution_access=access,
                    session_id=session_id, session_root=session_root, session_workspace=workspace))[0]
        assert result.error_code == "phase_disallowed"
    assert file.read_text(encoding="utf-8") == "original"


def test_extreme_delete_guard_preserves_volume_project_and_home(tmp_path, monkeypatch):
    project, external = folders(tmp_path)
    context = ToolContext(cwd=project, timeout_seconds=1)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: external))
    assert protected_delete_reason(Path(project.anchor), context) == "delete_volume_root_protected"
    assert protected_delete_reason(project, context) == "delete_root_protected"
    assert protected_delete_reason(project.parent, context) == "delete_root_protected"
    assert protected_delete_reason(external, context) == "delete_home_protected"
    assert protected_delete_reason(external / "ordinary-subdir", context) is None
