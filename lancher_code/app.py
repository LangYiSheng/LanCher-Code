from __future__ import annotations

from pathlib import Path

from rich.console import Console

from lancher_code.config import load_config, resolve_config_bootstrap_state
from lancher_code.errors import ConfigError
from lancher_code.providers.factory import create_provider
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tui_views.bootstrap import ConfigBootstrapTUI
from lancher_code.tui_views.chat import ChatTUI
from lancher_code.turn_runner import TurnRunner

DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0


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
        console.print(f"[错误] {exc.user_message}", style="bold red")
        return 1

    provider = create_provider(config.provider)
    cwd = Path.cwd()
    session_controller = SessionController(
        config.provider,
        cwd=cwd,
        plan_file_path=Path(config.runtime.plan_file_path),
    )
    tool_registry = create_default_tool_registry()
    tool_executor = ToolExecutor(
        tool_registry,
        cwd=cwd,
        timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
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
    return await tui.run()
