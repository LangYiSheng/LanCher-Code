from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.models import PermissionResolution, PermissionRule
from lancher_code.permissions.storage import PermissionRuleFileError, PermissionStorage
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.delete_file import DeleteFileTool
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.file_state_cache import FileStateCache


def _call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(0, "security-call", name, arguments, "{}")


def _context(project: Path, *, policy: str = "default") -> ToolContext:
    return ToolContext(
        cwd=project, project_root=project, timeout_seconds=2,
        permission_policy=policy, file_state_cache=FileStateCache(),  # type: ignore[arg-type]
    )


def _command_check(engine: PermissionEngine, project: Path, command: str, *, cwd: str = ".", policy: str = "default"):
    return engine.evaluate(
        call=_call("run_command", {"command": command, "description": "验证权限边界", "cwd": cwd}),
        tool=RunCommandTool().definition, context=_context(project, policy=policy),
    )


@pytest.mark.parametrize(("policy", "decision"), [("default", "ask"), ("acceptEdits", "allow")])
def test_single_file_delete_follows_edit_policy(tmp_path: Path, policy: str, decision: str) -> None:
    (tmp_path / "temporary.txt").write_text("temporary", encoding="utf-8")

    check = PermissionEngine().evaluate(
        call=_call("delete_file", {"path": "temporary.txt"}),
        tool=DeleteFileTool().definition, context=_context(tmp_path, policy=policy),
    )

    assert check.decision == decision
    if decision == "ask":
        assert check.request is not None and check.request.kind == "file_edit"
        assert "temporary.txt" in "\n".join(check.request.file_paths)


@pytest.mark.parametrize("policy", ["default", "acceptEdits", "bypass"])
@pytest.mark.parametrize("outcome", ["allow_session", "allow_project"])
def test_recursive_delete_always_requires_once_only_approval(tmp_path: Path, policy: str, outcome: str) -> None:
    directory = tmp_path / "temporary"
    directory.mkdir()
    (directory / "first.txt").write_text("first", encoding="utf-8")
    (directory / "second.txt").write_text("second", encoding="utf-8")
    storage = PermissionStorage(project_rules_path=tmp_path / ".lancher" / "permissions.yaml")
    storage.add_session_rule("DeleteFile(*)", "allow", match_kind="glob")
    before = storage.rules_for_scope("session")
    engine = PermissionEngine(storage)

    check = engine.evaluate(
        call=_call("delete_file", {"path": "temporary", "recursive": True}),
        tool=DeleteFileTool().definition, context=_context(tmp_path, policy=policy),
    )

    assert check.decision == "ask" and check.request is not None
    assert check.request.metadata["allow_once_only"] is True
    assert check.request.session_rule is None and check.request.project_rule is None
    preview = check.request.details + "\n" + "\n".join(check.request.file_paths)
    assert "first.txt" in preview and "second.txt" in preview
    engine.apply_resolution(check.request, PermissionResolution(check.request.request_id, outcome))  # type: ignore[arg-type]
    assert storage.rules_for_scope("session") == before
    assert storage.rules_for_scope("project") == []
    assert not (tmp_path / ".lancher" / "permissions.yaml").exists()


@pytest.mark.parametrize("relative", [".", ".git", ".lancher/sessions/" + "a" * 32 + "/events.jsonl"])
@pytest.mark.parametrize("policy", ["default", "acceptEdits", "bypass"])
def test_delete_cannot_remove_project_or_control_records(tmp_path: Path, relative: str, policy: str) -> None:
    (tmp_path / ".git").mkdir()
    record = tmp_path / ".lancher" / "sessions" / ("a" * 32) / "events.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text("record", encoding="utf-8")

    check = PermissionEngine().evaluate(
        call=_call("delete_file", {"path": relative, "recursive": True}),
        tool=DeleteFileTool().definition, context=_context(tmp_path, policy=policy),
    )

    assert check.decision == "deny"
    assert record.read_text(encoding="utf-8") == "record"


@pytest.mark.parametrize("command", [
    'Write-Output "format"', 'Write-Output "a > b"',
    "Remove-Item -LiteralPath temporary.txt", "Write-Output ready\nRemove-Item temporary.txt",
])
def test_normal_delete_and_quoted_text_are_approvable(tmp_path: Path, command: str) -> None:
    check = _command_check(PermissionEngine(), tmp_path, command)

    assert check.decision == "ask" and check.request is not None
    assert check.request.command == command


@pytest.mark.parametrize("separator", ["; ", "\n", " | "])
def test_command_glob_does_not_authorize_unmatched_subcommand(tmp_path: Path, separator: str) -> None:
    storage = PermissionStorage()
    storage.add_session_rule("RunCommand(git *)", "allow", match_kind="glob")
    engine = PermissionEngine(storage)

    assert _command_check(engine, tmp_path, "git status").decision == "allow"
    assert _command_check(engine, tmp_path, f"git status{separator}Write-Output hello").decision == "ask"
    storage.add_session_rule("RunCommand(Write-Output *)", "allow", match_kind="glob")
    assert _command_check(engine, tmp_path, f"git status{separator}Write-Output hello").decision == "allow"


@pytest.mark.parametrize("command", [
    'powershell -Command "git status"', 'bash -c "git status"',
    "$target = 'temporary.txt'; Remove-Item $target", "& $command",
])
@pytest.mark.parametrize("policy", ["default", "bypass"])
def test_unknown_dynamic_or_nested_command_needs_once_only_approval(tmp_path: Path, command: str, policy: str) -> None:
    storage = PermissionStorage()
    storage.add_session_rule("RunCommand(*)", "allow", match_kind="glob")

    check = _command_check(PermissionEngine(storage), tmp_path, command, policy=policy)

    assert check.decision == "ask" and check.request is not None
    assert check.request.metadata["allow_once_only"] is True
    assert check.request.session_rule is None and check.request.project_rule is None


@pytest.mark.parametrize("template", [
    'Get-Content "{outside}"', 'Write-Output hello > "{outside}"',
    'Remove-Item -LiteralPath "{outside}"', 'Set-Location "{outside}"; Get-Content data.txt',
])
@pytest.mark.parametrize("policy", ["default", "bypass"])
def test_command_body_paths_and_redirections_respect_project_root(tmp_path: Path, template: str, policy: str) -> None:
    outside = (tmp_path.parent / "outside").as_posix()
    command = template.format(outside=outside)

    check = _command_check(PermissionEngine(), tmp_path, command, policy=policy)

    assert check.decision == "deny"
    assert check.reason_code == "path_outside_project"


def test_changed_command_directory_updates_relative_path_resolution(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()

    check = _command_check(PermissionEngine(), tmp_path, "Set-Location ..; Get-Content ../outside.txt", cwd="nested")

    assert check.decision == "deny" and check.reason_code == "path_outside_project"


def test_long_command_is_analyzed_beyond_display_prefix(tmp_path: Path) -> None:
    command = 'Write-Output "' + "ordinary " * 700 + '"; Get-Content ../outside.txt'

    check = _command_check(PermissionEngine(), tmp_path, command)

    assert check.decision == "deny" and check.reason_code == "path_outside_project"


def test_saved_shell_approval_preserves_context_and_exact_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "nested").mkdir()
    path = tmp_path / ".lancher" / "permissions.yaml"
    storage = PermissionStorage(project_rules_path=path)
    engine = PermissionEngine(storage)
    command = "Write-Output 'a  B'"
    check = _command_check(engine, tmp_path, command, cwd="nested")
    assert check.request is not None
    assert check.request.metadata["cwd"] == str((tmp_path / "nested").resolve())
    engine.apply_resolution(check.request, PermissionResolution(check.request.request_id, "allow_project"))

    restored = PermissionStorage(project_rules_path=path)
    rule = restored.rules_for_scope("project")[0]
    assert rule.command_cwd == str((tmp_path / "nested").resolve())
    assert rule.command_shell
    engine = PermissionEngine(restored)
    assert _command_check(engine, tmp_path, command, cwd="nested").decision == "allow"
    assert _command_check(engine, tmp_path, command, cwd=".").decision == "ask"
    assert _command_check(engine, tmp_path, "Write-Output 'a b'", cwd="nested").decision == "ask"
    monkeypatch.setattr("lancher_code.permissions.rules.current_command_shell", lambda: "other-shell")
    assert _command_check(engine, tmp_path, command, cwd="nested").decision == "ask"


def test_legacy_exact_shell_allow_does_not_reuse_unbound_approval(tmp_path: Path) -> None:
    storage = PermissionStorage()
    storage.replace_session_rules([PermissionRule("RunCommand(git status)", "allow", "session")])

    assert _command_check(PermissionEngine(storage), tmp_path, "git status").decision == "ask"


@pytest.mark.asyncio
async def test_approved_single_file_delete_executes(tmp_path: Path) -> None:
    target = tmp_path / "temporary.txt"
    target.write_text("temporary", encoding="utf-8")
    requests = []

    async def approve(request):
        requests.append(request)
        return PermissionResolution(request.request_id, "allow_once")

    result = await ToolExecutor(create_default_tool_registry(), cwd=tmp_path).execute_calls(
        [_call("delete_file", {"path": "temporary.txt"})], permission_resolver=approve,
    )

    assert len(requests) == 1
    assert not result[0].is_error
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["allow_session", "allow_project"])
async def test_changed_delete_target_does_not_delete_or_save_approval(tmp_path: Path, outcome: str) -> None:
    target = tmp_path / "temporary.txt"
    target.write_text("old", encoding="utf-8")
    storage = PermissionStorage(project_rules_path=tmp_path / ".lancher" / "permissions.yaml")

    async def approve(request):
        target.write_text("replacement is a different version", encoding="utf-8")
        return PermissionResolution(request.request_id, outcome)

    result = await ToolExecutor(create_default_tool_registry(), cwd=tmp_path, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("delete_file", {"path": "temporary.txt"})], permission_resolver=approve,
    )

    assert result[0].is_error
    assert result[0].error_code in {"permission_target_changed", "delete_target_changed", "resource_target_changed"}
    assert target.read_text(encoding="utf-8") == "replacement is a different version"
    assert storage.rules_for_scope("session") == [] and storage.rules_for_scope("project") == []
    assert not (tmp_path / ".lancher" / "permissions.yaml").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "supersede"])
async def test_stop_during_delete_approval_keeps_target_and_rules(tmp_path: Path, stop: str) -> None:
    target = tmp_path / "temporary.txt"
    target.write_text("keep", encoding="utf-8")
    storage = PermissionStorage()
    token = CancellationToken()
    interrupted = False

    async def approve(request):
        nonlocal interrupted
        if stop == "cancel":
            token.cancel()
        else:
            interrupted = True
        return PermissionResolution(request.request_id, "allow_session")

    operation = ToolExecutor(create_default_tool_registry(), cwd=tmp_path, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("delete_file", {"path": "temporary.txt"})], permission_resolver=approve,
        cancellation_token=token, should_interrupt=lambda: interrupted,
    )
    if stop == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        assert (await operation)[0].error_code == "steering_superseded"
    assert target.read_text(encoding="utf-8") == "keep"
    assert storage.rules_for_scope("session") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace_target", "cancel"])
async def test_delete_rechecks_after_started_callback_without_saving_stale_grant(tmp_path: Path, change: str) -> None:
    target = tmp_path / "temporary.txt"
    target.write_text("keep", encoding="utf-8")
    storage = PermissionStorage()
    token = CancellationToken()

    async def approve(request):
        return PermissionResolution(request.request_id, "allow_session")

    async def started(call):
        if change == "cancel":
            token.cancel()
        else:
            target.write_text("new replacement", encoding="utf-8")

    operation = ToolExecutor(create_default_tool_registry(), cwd=tmp_path, permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("delete_file", {"path": "temporary.txt"})], permission_resolver=approve,
        cancellation_token=token, on_call_started=started,
    )
    if change == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        result = (await operation)[0]
        assert result.is_error
        assert result.error_code in {"permission_target_changed", "delete_target_changed", "resource_target_changed"}
    assert target.exists()
    assert storage.rules_for_scope("session") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["token", "timeout"])
async def test_cancelled_tool_cleanup_result_preserves_partial_effects(tmp_path: Path, stop: str) -> None:
    token = CancellationToken()
    context = _context(tmp_path)
    context.cancellation_token = token
    expected = ToolExecutionResult(
        "partial-call", "delete_file", is_error=True, error_code="delete_interrupted",
        metadata={"partial": True, "deleted_count": 1, "automatic_retry": False},
    )

    async def cleanup_after_partial_commit():
        if stop == "token":
            token.cancel()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return expected

    actual = await ToolExecutor._await_cancelable(cleanup_after_partial_commit(), context,
                                                 timeout=0.01 if stop == "timeout" else None)

    assert actual is expected
    assert actual.metadata["partial"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["token", "task"])
async def test_recursive_delete_cancel_reports_deleted_files_through_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str) -> None:
    from lancher_code.filesystem import deletion

    directory = tmp_path / "temporary"
    directory.mkdir()
    (directory / "first.txt").write_text("first", encoding="utf-8")
    (directory / "second.txt").write_text("second", encoding="utf-8")
    token = CancellationToken()
    storage = PermissionStorage()
    reported = []
    original_unlink = Path.unlink
    original_sleep = asyncio.sleep
    parent_task = None

    def cancel_after_unlink(path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if path.parent == directory:
            token.cancel()
            if stop == "task":
                parent_task.cancel()

    async def pause_at_next_commit(delay, *args, **kwargs):
        if token.is_cancelled:
            # 让执行器的停止监听先醒来，确保覆盖取消收尾结果的返回路径。
            await asyncio.Event().wait()
        else:
            await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", cancel_after_unlink)
    monkeypatch.setattr(deletion.asyncio, "sleep", pause_at_next_commit)

    async def approve(request):
        return PermissionResolution(request.request_id, "allow_session")

    async def report(result):
        reported.append(result)

    parent_task = asyncio.create_task(ToolExecutor(create_default_tool_registry(), cwd=tmp_path,
                                 permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("delete_file", {"path": "temporary", "recursive": True})],
        permission_resolver=approve, cancellation_token=token, on_result=report,
    ))
    if stop == "task":
        with pytest.raises(asyncio.CancelledError):
            await parent_task
        results = reported
    else:
        results = await parent_task

    result = results[0]
    assert result.error_code == "delete_interrupted"
    assert result.metadata["partial"] is True
    assert result.metadata["deleted_count"] == 1
    assert result.metadata["automatic_retry"] is False
    assert len(list(directory.iterdir())) == 1
    assert all(not Path(path).exists() for path in result.metadata["deleted_paths"])
    assert reported == [result]
    assert storage.rules_for_scope("session") == []


@pytest.mark.asyncio
async def test_successful_tool_retains_success_when_saving_grant_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = PermissionStorage(project_rules_path=tmp_path / ".lancher" / "permissions.yaml")

    def fail_to_save(path, rules):
        raise PermissionRuleFileError("模拟授权记录保存失败")

    monkeypatch.setattr(storage, "_write_rules", fail_to_save)

    async def approve(request):
        return PermissionResolution(request.request_id, "allow_project")

    result = (await ToolExecutor(create_default_tool_registry(), cwd=tmp_path,
                                permission_engine=PermissionEngine(storage)).execute_calls(
        [_call("write_file", {"path": "created.txt", "content": "committed"})],
        permission_resolver=approve,
    ))[0]

    assert not result.is_error
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "committed"
    assert "模拟授权记录保存失败" in result.metadata["permission_rule_save_error"]
    assert "操作已完成" in result.content
    assert storage.rules_for_scope("project") == []


@pytest.mark.asyncio
async def test_accept_edits_rechecks_resource_path_after_started_callback(tmp_path: Path) -> None:
    async def change_target(call):
        call.arguments["path"] = "replacement.txt"

    result = (await ToolExecutor(create_default_tool_registry(), cwd=tmp_path).execute_calls(
        [_call("write_file", {"path": "original.txt", "content": "must not write"})],
        permission_policy="acceptEdits", on_call_started=change_target,
    ))[0]

    assert result.error_code == "resource_target_changed"
    assert not (tmp_path / "original.txt").exists()
    assert not (tmp_path / "replacement.txt").exists()
