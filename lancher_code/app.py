from __future__ import annotations

from pathlib import Path

from rich.console import Console

from lancher_code.config import (
    get_global_permissions_path,
    get_project_permissions_path,
    load_config,
    resolve_config_bootstrap_state,
)
from lancher_code.errors import ConfigError
from lancher_code.config_system.paths import get_global_mcp_config_path, get_project_mcp_config_path
from lancher_code.mcp import MCPClientManager, load_mcp_config
from lancher_code.logging_system import get_logger, register_sensitive_values
from lancher_code.permission_engine import PermissionEngine, PermissionStorage
from lancher_code.providers.factory import create_provider
from lancher_code.session import SessionController
from lancher_code.settings_service import SettingsService
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tui_views.bootstrap import ConfigBootstrapTUI
from lancher_code.tui_views.chat import ChatTUI
from lancher_code.turn_runner import TurnRunner

DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
logger = get_logger("app")


async def run_app() -> int:
    console = Console()
    bootstrap_state = resolve_config_bootstrap_state()
    if bootstrap_state.needs_setup:
        setup_completed = await ConfigBootstrapTUI(bootstrap_state.config_path).run()
        if not setup_completed:
            return 0

    try:
        config = load_config(bootstrap_state.config_path)
    except ConfigError as exc:
        logger.error("event=application_config_invalid exception_type=%s", type(exc).__name__)
        console.print(f"[错误] {exc.user_message}", style="bold red")
        return 1

    provider = create_provider(config.provider)
    register_sensitive_values([config.provider.api_key])
    cwd = Path.cwd()
    permission_storage = PermissionStorage(
        project_rules_path=get_project_permissions_path(cwd),
        user_rules_path=get_global_permissions_path(),
    )
    session_controller = SessionController(
        config.provider,
        cwd=cwd,
        plan_file_path=Path(config.runtime.plan_file_path),
        initial_runtime_mode=config.runtime.permission_mode,
        permission_storage=permission_storage,
    )
    tool_registry = create_default_tool_registry()
    mcp_configs, mcp_issues = load_mcp_config(cwd)
    register_sensitive_values(
        value
        for mcp_config in mcp_configs
        for value in (*mcp_config.env.values(), *mcp_config.headers.values())
    )
    mcp_manager = MCPClientManager(
        mcp_configs,
        issues=mcp_issues,
        timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
    )
    permission_engine = PermissionEngine(permission_storage)
    settings_service = SettingsService(
        config_path=bootstrap_state.config_path,
        global_mcp_path=get_global_mcp_config_path(),
        project_mcp_path=get_project_mcp_config_path(cwd),
        permission_storage=permission_engine.storage,
    )
    tool_executor = ToolExecutor(
        tool_registry,
        cwd=cwd,
        timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
        permission_engine=permission_engine,
    )
    turn_runner = TurnRunner(
        provider,
        session_controller,
        tool_registry,
        tool_executor,
        max_tool_loops=config.runtime.tool_loop_limit,
        unknown_tool_streak_limit=config.runtime.unknown_tool_streak_limit,
    )
    tui = ChatTUI(
        turn_runner=turn_runner,
        provider_config=config.provider,
        session_controller=session_controller,
        ui_config=config.ui,
    )
    if hasattr(tui, "configure_settings"):
        tui.configure_settings(settings_service)
    if hasattr(tui, "configure_mcp"):
        tui.configure_mcp(mcp_manager, tool_registry)
    try:
        return await tui.run()
    finally:
        await mcp_manager.close()
