from __future__ import annotations

from pathlib import Path

GLOBAL_CONFIG_DIRNAME = ".lancher"
GLOBAL_CONFIG_FILENAME = "lancher.yaml"
PERMISSIONS_CONFIG_FILENAME = "permissions.yaml"
MCP_CONFIG_FILENAME = "mcp.yaml"
DEFAULT_PLAN_FILE_PATH = "./.lancher/plan.md"


def get_global_config_dir(home_dir: Path | None = None) -> Path:
    root = (home_dir or Path.home()).expanduser()
    return root / GLOBAL_CONFIG_DIRNAME


def get_global_config_path(home_dir: Path | None = None) -> Path:
    return get_global_config_dir(home_dir) / GLOBAL_CONFIG_FILENAME


def get_global_permissions_path(home_dir: Path | None = None) -> Path:
    return get_global_config_dir(home_dir) / PERMISSIONS_CONFIG_FILENAME


def get_global_mcp_config_path(home_dir: Path | None = None) -> Path:
    return get_global_config_dir(home_dir) / MCP_CONFIG_FILENAME


def get_legacy_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve() / GLOBAL_CONFIG_FILENAME


def get_project_permissions_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve() / GLOBAL_CONFIG_DIRNAME / PERMISSIONS_CONFIG_FILENAME


def get_project_mcp_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve() / GLOBAL_CONFIG_DIRNAME / MCP_CONFIG_FILENAME
