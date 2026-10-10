from __future__ import annotations

from lancher_code.contracts.control import validate_runtime_axes

from dataclasses import dataclass
from lancher_code.contracts.control import CancellationToken
from lancher_code.contracts.control import PermissionPolicy
from lancher_code.contracts.control import WorkPhase
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lancher_code.execution.runtime import ExecutionRuntime
    from lancher_code.execution.scheduler import ResourceLease
    from lancher_code.tools.core.file_state_cache import FileStateCache


@dataclass(slots=True)
class ToolContext:
    cwd: Path
    timeout_seconds: float
    project_root: Path | None = None
    plan_file_path: Path | None = None
    session_id: str | None = None
    session_workspace: Path | None = None
    session_root: Path | None = None
    cancellation_token: CancellationToken | None = None
    file_state_cache: "FileStateCache | None" = None
    work_phase: WorkPhase = "execute"
    permission_policy: PermissionPolicy = "default"
    execution_runtime: "ExecutionRuntime | None" = None
    turn_id: str | None = None
    invocation_id: str | None = None
    generation: int = 0
    resource_lease: "ResourceLease | None" = None

    def __post_init__(self) -> None:
        validate_runtime_axes(self.work_phase, self.permission_policy)
        if self.project_root is None:
            self.project_root = self.cwd.resolve()
