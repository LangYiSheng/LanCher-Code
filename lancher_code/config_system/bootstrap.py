from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lancher_code.config_system.paths import get_global_config_path, get_legacy_config_path


@dataclass(slots=True)
class ConfigBootstrapState:
    config_path: Path
    needs_setup: bool
    legacy_config_path: Path
    legacy_config_exists: bool


def resolve_config_bootstrap_state(
    *,
    home_dir: Path | None = None,
    cwd: Path | None = None,
) -> ConfigBootstrapState:
    config_path = get_global_config_path(home_dir).resolve()
    legacy_config_path = get_legacy_config_path(cwd)
    return ConfigBootstrapState(
        config_path=config_path,
        needs_setup=not config_path.exists(),
        legacy_config_path=legacy_config_path,
        legacy_config_exists=legacy_config_path.exists(),
    )
