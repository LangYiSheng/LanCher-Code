from __future__ import annotations

from dataclasses import dataclass, field
from lancher_code.contracts.control import ExecutionAccess, WorkPhase
from typing import Literal


PermissionMatchKind = Literal["exact", "glob"]


RuleScope = Literal["session", "project", "user"]


PermissionDecision = Literal["allow", "deny", "ask"]


PermissionRuleResult = Literal["deny"]


PermissionRequestKind = Literal["command", "file_edit", "external_tool"]


PermissionResolutionOutcome = Literal["allow_once", "allow_session", "deny", "superseded"]


@dataclass(slots=True, frozen=True)
class DirectoryGrant:
    """本会话已批准的真实绝对路径范围；write 同时包含读取。"""

    path: str
    access: Literal["read", "write"] = "read"
    recursive: bool = True


@dataclass(slots=True)
class PermissionRule:
    match: str
    result: PermissionRuleResult
    scope: RuleScope
    match_kind: PermissionMatchKind = "exact"
    command_cwd: str | None = None
    command_shell: str | None = None


@dataclass(slots=True)
class PermissionRequest:
    request_id: str
    call_id: str
    tool_name: str
    tool_label: str
    kind: PermissionRequestKind
    title: str
    prompt: str
    details: str
    command: str | None = None
    description: str | None = None
    file_paths: list[str] = field(default_factory=list)
    preview_lines: list[dict[str, str]] = field(default_factory=list)
    directory_grants: list[DirectoryGrant] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    work_phase: WorkPhase = "execute"
    execution_access: ExecutionAccess = "project"


@dataclass(slots=True)
class PermissionResolution:
    request_id: str
    outcome: PermissionResolutionOutcome


@dataclass(slots=True)
class PermissionCheck:
    decision: PermissionDecision
    reason_code: str | None = None
    reason_message: str | None = None
    request: PermissionRequest | None = None
    metadata: dict[str, object] | None = None
