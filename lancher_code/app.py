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
from lancher_code.execution.runtime import ExecutionRuntime
from lancher_code.model_catalog import resolve_model
from lancher_code.config_system.paths import get_global_mcp_config_path, get_project_mcp_config_path
from lancher_code.mcp import MCPClientManager, load_mcp_config
from lancher_code.logging_system import get_logger, register_sensitive_values
from lancher_code.permission_engine import PermissionEngine, PermissionStorage
from lancher_code.providers.factory import create_provider
from lancher_code.run_usage import RunUsageTracker
from lancher_code.run_summary import print_exit_summary, select_resume_target
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

    active_config = resolve_model(config)
    usage_tracker = RunUsageTracker()

    def provider_factory(provider_config):
        # 新建、恢复、模型热切换与压缩都共享本次启动的账本。
        return create_provider(provider_config, usage_observer=usage_tracker)

    provider = provider_factory(active_config)
    register_sensitive_values([active_config.api_key])
    cwd = Path.cwd()
    permission_storage = PermissionStorage(
        project_rules_path=get_project_permissions_path(cwd),
        user_rules_path=get_global_permissions_path(),
    )
    session_controller = SessionController(
        active_config,
        cwd=cwd,
        initial_work_phase=config.runtime.work_phase,
        initial_permission_policy=config.runtime.permission_policy,
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
        execution_runtime=ExecutionRuntime(cwd, config.execution),
    )
    turn_runner = TurnRunner(
        provider,
        session_controller,
        tool_registry,
        tool_executor,
        max_tool_loops=config.runtime.tool_loop_limit,
        unknown_tool_streak_limit=config.runtime.unknown_tool_streak_limit,
    )
    turn_runner.configure_models(config, provider_factory=provider_factory)
    tui = ChatTUI(
        turn_runner=turn_runner,
        provider_config=active_config,
        session_controller=session_controller,
        ui_config=config.ui,
    )
    if hasattr(tui, "configure_settings"):
        tui.configure_settings(settings_service)
    if hasattr(tui, "configure_mcp"):
        tui.configure_mcp(mcp_manager, tool_registry)
    normal_return = False
    cleanup_errors: list[str] = []
    stopped_processes = 0
    target = None
    try:
        result = await tui.run()
        normal_return = True
    finally:
        stopped_processes = max(tui.stopped_process_count, turn_runner.application_process_count)
        try:
            await turn_runner.shutdown()
        except Exception as exc:
            logger.exception("event=application_runner_shutdown_failed")
            cleanup_errors.append(f"任务收尾失败：{exc}")
        try:
            session_controller.close()
        except Exception as exc:
            logger.exception("event=application_session_close_failed")
            cleanup_errors.append(f"会话保存失败：{exc}")
        try:
            await mcp_manager.close()
        except Exception as exc:
            logger.exception("event=application_mcp_close_failed")
            cleanup_errors.append(f"连接关闭失败：{exc}")
        if normal_return:
            try:
                target = select_resume_target(session_controller)
            except (ValueError, OSError) as exc:
                logger.exception("event=application_resume_target_failed")
                cleanup_errors.append(f"恢复信息读取失败：{exc}")
            print_exit_summary(console, project_root=cwd, target=target, usage=usage_tracker.snapshot(),
                               cleanup_errors=tuple(cleanup_errors), stopped_processes=stopped_processes)
        elif cleanup_errors:
            for error in cleanup_errors:
                console.print(error, markup=False, style="yellow")
    return 1 if cleanup_errors else result
