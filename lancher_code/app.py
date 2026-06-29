from __future__ import annotations

from pathlib import Path

from rich.console import Console

from lancher_code.config import load_config
from lancher_code.errors import ConfigError
from lancher_code.providers.factory import create_provider
from lancher_code.session import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.turn_runner import TurnRunner
from lancher_code.tui import ChatTUI

DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0


async def run_app(config_path: str) -> int:
    console = Console()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.print(f"[错误] {exc.user_message}", style="bold red")
        return 1

    provider = create_provider(config.provider)
    cwd = Path.cwd()
    session_controller = SessionController(config.provider, cwd=cwd)
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
    )
    tui = ChatTUI(
        turn_runner=turn_runner,
        provider_config=config.provider,
        session_controller=session_controller,
        ui_config=config.ui,
    )
    return await tui.run()
