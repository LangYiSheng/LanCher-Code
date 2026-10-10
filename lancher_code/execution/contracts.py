"""执行层共享契约：数据可以保存，系统句柄只存在于后端。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


ResourceKind = Literal["path", "process", "project", "external"]
ResourceMode = Literal["shared", "exclusive"]
ResourceLifetime = Literal["invocation", "process"]
ProcessLifetime = Literal["turn", "session"]
ProcessTransport = Literal["pipe", "pty"]


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    kind: ResourceKind
    key: str
    mode: ResourceMode = "exclusive"
    recursive: bool = False
    lifetime: ResourceLifetime = "process"


@dataclass(frozen=True, slots=True)
class ResourceOwner:
    """排队诊断只携带身份，不复制命令正文或进程输入。"""
    session_id: str | None = None
    invocation_id: str | None = None
    tool_name: str | None = None
    process_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    max_concurrency: int = 8
    max_processes: int = 32
    max_processes_per_session: int = 8
    output_limit_bytes: int = 100 * 1024 * 1024
    max_read_chars: int = 16000
    stop_grace_seconds: float = 1.0
    drain_timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class ReadinessProbe:
    kind: Literal["tcp"] = "tcp"
    host: str = "127.0.0.1"
    port: int = 0
    timeout_ms: int = 30000


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    command: str
    description: str
    cwd: Path
    transport: ProcessTransport = "pipe"
    lifetime: ProcessLifetime = "turn"
    yield_ms: int = 1000
    max_runtime_ms: int | None = None
    readiness: ReadinessProbe | None = None
    columns: int = 100
    rows: int = 30


@dataclass(slots=True)
class ProcessInfo:
    process_id: str
    session_id: str
    origin_turn_id: str | None
    origin_invocation_id: str
    command: str
    description: str
    cwd: str
    transport: ProcessTransport
    lifetime: ProcessLifetime
    status: str = "starting"
    readiness: str = "unknown"
    pid: int | None = None
    started_at: str | None = None
    updated_at: str | None = None
    exit_code: int | None = None
    exit_reason: str | None = None
    output_chars: int = 0
    output_bytes: int = 0
    storage_error: str | None = None
    input_bytes: int = 0
    input_status: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OutputPage:
    text: str = ""
    stdout: str = ""
    stderr: str = ""
    cursor: int = 0
    next_cursor: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class InvocationInfo:
    invocation_id: str
    provider_call_id: str
    session_id: str
    turn_id: str | None
    generation: int
    tool_name: str
    state: str = "queued"
    started_at: str | None = None
    updated_at: str | None = None
    error_code: str | None = None
    process_id: str | None = None

    waiting: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommandProfile:
    """用户配置的命令资源约定，不是对任意 Shell 副作用的自动证明。"""
    name: str
    command_match: str
    resources: tuple[ResourceClaim, ...] = ()
    readiness: ReadinessProbe | None = None


@dataclass(slots=True)
class ExecutionConfig:
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    command_profiles: list[CommandProfile] = field(default_factory=list)
