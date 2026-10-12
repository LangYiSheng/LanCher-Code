"""TUI 应用入口：加载界面配置并消费独立 Agent SDK。"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from lancher_code.config.bootstrap import resolve_config_bootstrap_state
from lancher_code.config.loader import load_config
from lancher_code.config.paths import get_global_mcp_config_path, get_project_mcp_config_path
from lancher_code.config.settings import SettingsService
from lancher_code.errors import ConfigError
from lancher_code.logging_system import get_logger
from lancher_code.permissions.storage import PermissionRuleFileError
from lancher_code.sdk import AgentShutdownError, create_agent
from lancher_code.tui.app import ChatTUI
from lancher_code.tui.bootstrap import ConfigBootstrapTUI
from lancher_code.tui.exit_summary import print_exit_summary, select_resume_target

logger = get_logger("app")


async def run_app() -> int:
    console = Console()
    bootstrap = resolve_config_bootstrap_state()
    if bootstrap.needs_setup:
        if not await ConfigBootstrapTUI(bootstrap.config_path).run():
            return 0
    cwd = Path.cwd()
    try:
        config = load_config(bootstrap.config_path)
        agent = create_agent(config.to_agent_config(), cwd=cwd)
    except (ConfigError, PermissionRuleFileError) as exc:
        logger.error("event=application_config_invalid exception_type=%s", type(exc).__name__)
        console.print(f"[错误] {exc.user_message}", markup=False, style="bold red")
        return 1

    normal_return = False
    cleanup_errors: list[str] = []
    stopped_processes = 0
    tui = None
    try:
        settings = SettingsService(config_path=bootstrap.config_path,
            global_mcp_path=get_global_mcp_config_path(), project_mcp_path=get_project_mcp_config_path(cwd),
            permission_storage=agent.permission_storage)
        tui = ChatTUI(agent=agent, ui_config=config.ui, settings_service=settings)
        agent.start_capabilities()
        result = await tui.run()
        normal_return = True
    finally:
        stopped_processes = max(tui.stopped_process_count if tui is not None else 0,
                                agent.application_process_count)
        try:
            await agent.close()
        except AgentShutdownError as exc:
            cleanup_errors.extend(exc.errors)
        if normal_return:
            target = None
            try:
                target = select_resume_target(agent)
            except (ValueError, OSError) as exc:
                logger.exception("event=application_resume_target_failed")
                cleanup_errors.append(f"恢复信息读取失败：{exc}")
            print_exit_summary(console, project_root=cwd, target=target, usage=agent.run_usage(),
                               cleanup_errors=tuple(cleanup_errors), stopped_processes=stopped_processes)
        elif cleanup_errors:
            for error in cleanup_errors:
                console.print(error, markup=False, style="yellow")
    return 1 if cleanup_errors else result
