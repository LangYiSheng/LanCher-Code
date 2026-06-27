from __future__ import annotations

from rich.console import Console

from lancher_code.config import load_config
from lancher_code.errors import ConfigError
from lancher_code.providers.factory import create_provider
from lancher_code.session import SessionController
from lancher_code.tui import ChatTUI


async def run_app(config_path: str) -> int:
    console = Console()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.print(f"[错误] {exc.user_message}", style="bold red")
        return 1

    provider = create_provider(config.provider)
    session_controller = SessionController(config.provider)
    tui = ChatTUI(
        provider=provider,
        provider_config=config.provider,
        session_controller=session_controller,
        ui_config=config.ui,
    )
    return await tui.run()
