from __future__ import annotations

from dataclasses import dataclass, field
from lancher_code.contracts.control import PermissionPolicy, WorkPhase
from typing import Literal


PermissionMatchKind = Literal["exact", "glob"]


RuleScope = Literal["session", "project", "user"]


PermissionDecision = Literal["allow", "deny", "ask"]


PermissionRuleResult = Literal["allow", "deny"]


PermissionRequestKind = Literal["command", "file_edit", "external_tool"]


PermissionResolutionOutcome = Literal["allow_once", "allow_session", "allow_project", "deny", "superseded"]


@dataclass(slots=True)
class PermissionRule:
    match: str
    result: PermissionRuleResult
    scope: RuleScope
    match_kind: PermissionMatchKind = "exact"


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
    session_rule: str | None = None
    project_rule: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    match_kind: PermissionMatchKind = "exact"
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"


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
