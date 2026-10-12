"""只装配 Agent 核心，不启动界面或交互式配置。"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from lancher_code.agent.runner import TurnRunner
from lancher_code.agent.service import AgentService
from lancher_code.agent.skills import SkillsService
from lancher_code.config.models import AgentConfig
from lancher_code.config.paths import get_global_permissions_path, get_project_permissions_path
from lancher_code.execution.runtime import ExecutionRuntime
from lancher_code.logging_system import register_sensitive_values
from lancher_code.mcp import MCPClientManager, load_mcp_config
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.catalog import resolve_model
from lancher_code.providers.factory import create_provider
from lancher_code.providers.models import ProviderConfig
from lancher_code.sessions.controller import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.usage.ledger import RunUsageTracker


def create_agent(
    config: AgentConfig,
    *,
    cwd: Path | str | None = None,
    model_ref: str | None = None,
    provider_factory: Callable[[ProviderConfig], ChatProvider] | None = None,
    tool_registry: ToolRegistry | None = None,
    skills_service: SkillsService | None = None,
    mcp_manager: MCPClientManager | None = None,
    permission_storage: PermissionStorage | None = None,
    enable_mcp: bool = True,
) -> AgentService:
    """显式配置创建一个会话运行服务；通过 async with 管理它的生命期。"""
    if not isinstance(config, AgentConfig):
        raise TypeError("create_agent 需要 AgentConfig；应用配置请先调用 to_agent_config()。")
    snapshot = deepcopy(config)
    root = (Path(cwd) if cwd is not None else Path.cwd()).resolve()
    if not root.is_dir():
        raise ValueError(f"项目目录不存在：{root}")
    if (type(snapshot.runtime.tool_loop_limit) is not int or type(snapshot.runtime.unknown_tool_streak_limit) is not int
            or snapshot.runtime.tool_loop_limit < 1 or snapshot.runtime.unknown_tool_streak_limit < 1):
        raise ValueError("工具循环和未知工具次数上限必须为正整数。")
    selected = model_ref or snapshot.default_model
    active = resolve_model(snapshot.providers, selected)
    register_sensitive_values([active.api_key])
    tracker = RunUsageTracker()
    def make_provider(provider_config: ProviderConfig) -> ChatProvider:
        return (provider_factory(provider_config) if provider_factory is not None
                else create_provider(provider_config, usage_observer=tracker))
    provider = make_provider(active)
    storage = permission_storage if permission_storage is not None else PermissionStorage(
        project_rules_path=get_project_permissions_path(root), user_rules_path=get_global_permissions_path(),
    )
    registry = tool_registry if tool_registry is not None else create_default_tool_registry()
    manager = mcp_manager
    if manager is None and enable_mcp:
        configs, issues = load_mcp_config(root)
        register_sensitive_values(value for item in configs for value in (*item.env.values(), *item.headers.values()))
        manager = MCPClientManager(configs, issues=issues)
    session = SessionController(active, cwd=root,
        initial_work_phase=snapshot.runtime.work_phase,
        initial_execution_access=snapshot.runtime.execution_access,
        permission_storage=storage, usage_tracker=tracker,
    )
    executor = ToolExecutor(registry, cwd=root, timeout_seconds=10.0,
        permission_engine=PermissionEngine(storage), execution_runtime=ExecutionRuntime(root, snapshot.execution))
    runner = TurnRunner(provider, session, registry, executor,
        max_tool_loops=snapshot.runtime.tool_loop_limit,
        unknown_tool_streak_limit=snapshot.runtime.unknown_tool_streak_limit,
        skills_service=skills_service, mcp_manager=manager,
    )
    runner.configure_models(snapshot, provider_factory=make_provider, model_ref=selected)
    return AgentService(runner, session, usage_tracker=tracker)
