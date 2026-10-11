from __future__ import annotations

import json
from pathlib import Path

import pytest

from lancher_code.permissions.models import DirectoryGrant, PermissionRule
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.models import SessionState
from lancher_code.sessions.storage import SessionRepositoryError


def test_session_codec_roundtrip_preserves_shell_denial_context(tmp_path: Path) -> None:
    rule = PermissionRule(
        "RunCommand(Write-Output 'a  B')", "deny", "session", "exact",
        command_cwd=str(tmp_path.resolve()), command_shell="powershell",
    )
    snapshot = SessionCodec.encode(SessionState(), [], [rule], None)

    restored = SessionCodec.decode(json.loads(json.dumps(snapshot)), "a" * 32)

    assert restored[2] == [rule]
    assert restored[2][0].match == "RunCommand(Write-Output 'a  B')"


@pytest.mark.parametrize(("cwd", "shell"), [
    (None, "powershell"), ("/project", None), (123, "powershell"), ("/project", ""),
])
def test_session_codec_rejects_incomplete_or_invalid_command_context(cwd, shell) -> None:
    snapshot = SessionCodec.encode(SessionState(), [], [], None)
    snapshot["rules"] = [{
        "match": "RunCommand(git status)", "result": "deny", "scope": "session",
        "match_kind": "exact", "command_cwd": cwd, "command_shell": shell,
    }]

    with pytest.raises(SessionRepositoryError, match="会话状态无效"):
        SessionCodec.decode(snapshot, "a" * 32)


def test_session_resume_retains_saved_shell_and_directory(openai_provider_config, tmp_path: Path) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    storage = PermissionStorage()
    controller = SessionController(openai_provider_config, cwd=tmp_path, permission_storage=storage)
    try:
        controller.create_user_message("保存命令拒绝规则")
        storage.add_session_rule(
            "RunCommand(git status)", "deny", command_cwd=str(directory.resolve()), command_shell="powershell",
        )
        saved_id = controller.session_id
        assert saved_id is not None
    finally:
        controller.close()

    restored_storage = PermissionStorage()
    restored = SessionController(openai_provider_config, cwd=tmp_path, permission_storage=restored_storage)
    try:
        assert restored.resume_session(saved_id) == 1
        assert restored_storage.rules_for_scope("session") == [PermissionRule(
            "RunCommand(git status)", "deny", "session", "exact",
            command_cwd=str(directory.resolve()), command_shell="powershell",
        )]
    finally:
        restored.close()


def test_session_restores_directory_grant_and_clears_it_for_new_session(openai_provider_config, tmp_path):
    external = tmp_path / 'external'
    project = tmp_path / 'project'
    external.mkdir()
    project.mkdir()
    storage = PermissionStorage()
    controller = SessionController(openai_provider_config, cwd=project, permission_storage=storage)
    controller.create_user_message('批准外部目录')
    grant = DirectoryGrant(str(external.resolve()), 'write', True)
    storage.add_directory_grants([grant])
    controller.set_execution_access('full')
    controller.set_work_phase('plan')
    session_id = controller.session_id
    controller.close()
    restored = SessionController(openai_provider_config, cwd=project)
    try:
        restored.resume_session(session_id)
        assert restored._permission_storage.session_grants == [grant]
        assert restored.work_phase == 'plan' and restored.execution_access == 'full'
        restored.set_work_phase('execute')
        assert restored.execution_access == 'full'
        restored.new_session()
        assert restored._permission_storage.session_grants == []
        assert restored.state.directory_grants == []
    finally:
        restored.close()


def test_new_controller_does_not_inherit_previous_storage_directory_grants(openai_provider_config, tmp_path):
    storage = PermissionStorage()
    storage.add_directory_grants([DirectoryGrant(str(tmp_path.resolve()), 'write')])
    controller = SessionController(openai_provider_config, cwd=tmp_path, permission_storage=storage)
    try:
        assert controller.state.directory_grants == []
        assert storage.session_grants == []
    finally:
        controller.close()


@pytest.mark.parametrize('grant', [
    {'path': 'relative', 'access': 'read', 'recursive': True},
    {'path': 'C:/absolute', 'access': 'allow', 'recursive': True},
    {'path': 'C:/absolute', 'access': 'read', 'recursive': 'true'},
])
def test_session_rejects_invalid_directory_grants(grant):
    snapshot = SessionCodec.encode(SessionState(), [], [], None)
    snapshot['state']['directory_grants'] = [grant]
    with pytest.raises(SessionRepositoryError, match='目录授权'):
        SessionCodec.decode(snapshot, 'a' * 32)
