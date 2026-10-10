from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lancher_code.config.paths import get_global_config_path


@dataclass(slots=True)
class ConfigBootstrapState:
    config_path: Path
    needs_setup: bool


def resolve_config_bootstrap_state(
    *,
    home_dir: Path | None = None,
) -> ConfigBootstrapState:
    config_path = get_global_config_path(home_dir).resolve()
    return ConfigBootstrapState(
        config_path=config_path,
        needs_setup=not config_path.exists(),
    )
