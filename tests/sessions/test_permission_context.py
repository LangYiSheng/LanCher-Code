from __future__ import annotations

import json
from pathlib import Path

import pytest

from lancher_code.permissions.models import PermissionRule
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.models import SessionState
from lancher_code.sessions.storage import SessionRepositoryError


def test_session_codec_roundtrip_preserves_shell_approval_context(tmp_path: Path) -> None:
    rule = PermissionRule(
        "RunCommand(Write-Output 'a  B')", "allow", "session", "exact",
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
        "match": "RunCommand(git status)", "result": "allow", "scope": "session",
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
        controller.create_user_message("保存命令授权上下文")
        storage.add_session_rule(
            "RunCommand(git status)", "allow", command_cwd=str(directory.resolve()), command_shell="powershell",
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
            "RunCommand(git status)", "allow", "session", "exact",
            command_cwd=str(directory.resolve()), command_shell="powershell",
        )]
    finally:
        restored.close()
