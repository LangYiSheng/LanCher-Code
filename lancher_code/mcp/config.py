from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from lancher_code.config_system.paths import get_global_mcp_config_path, get_project_mcp_config_path
from lancher_code.logging_system import get_logger

logger = get_logger("mcp.config")

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(slots=True, frozen=True)
class MCPConfigIssue:
    source: str
    message: str
    server_name: str | None = None


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    type: Literal["stdio", "http"]
    enabled: bool = True
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_stdio(self) -> bool:
        return self.type == "stdio"


def load_mcp_config(project_root: Path, *, home_dir: Path | None = None, environ: dict[str, str] | None = None) -> tuple[list[MCPServerConfig], list[MCPConfigIssue]]:
    issues: list[MCPConfigIssue] = []
    user = _read_layer(get_global_mcp_config_path(home_dir), issues)
    project = _read_layer(get_project_mcp_config_path(project_root), issues)
    merged = {**user, **project}
    configs: list[MCPServerConfig] = []
    env_source = os.environ if environ is None else environ
    for name, raw in merged.items():
        try:
            config = _parse_server(name, raw, env_source)
        except ValueError as exc:
            issue = MCPConfigIssue("config", str(exc), str(name))
            issues.append(issue)
            logger.error("event=mcp_config_invalid server=%s reason=%s", name, issue.message)
            continue
        if config.enabled:
            configs.append(config)
    return configs, issues


def _read_layer(path: Path, issues: list[MCPConfigIssue]) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        issues.append(MCPConfigIssue("config", f"MCP 配置文件无法读取或 YAML 非法: {path}"))
        # YAML 解析异常可能内嵌原始配置行，这里只记录类型，避免凭据随坏行落盘。
        logger.error(
            "event=mcp_config_file_invalid path=%s exception_type=%s",
            path, type(exc).__name__,
        )
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(MCPConfigIssue("config", f"MCP 配置顶层必须是对象: {path}"))
        logger.error("event=mcp_config_top_level_invalid path=%s", path)
        return {}
    servers = raw.get("mcp_servers", {})
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        issues.append(MCPConfigIssue("config", f"mcp_servers 必须是对象: {path}"))
        logger.error("event=mcp_config_servers_invalid path=%s", path)
        return {}
    return dict(servers)


def _parse_server(name: object, raw: object, environ: dict[str, str]) -> MCPServerConfig:
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ValueError(f"MCP Server 名称不合法: {name!s}")
    if not isinstance(raw, dict):
        raise ValueError(f"Server {name} 配置必须是对象")
    server_type = raw.get("type")
    if server_type not in {"stdio", "http"}:
        raise ValueError(f"Server {name}.type 只能是 stdio 或 http")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"Server {name}.enabled 必须是布尔值")
    if not enabled:
        return MCPServerConfig(name=name, type=server_type, enabled=False)
    if server_type == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"Server {name}.command 必须是非空字符串")
        return MCPServerConfig(name=name, type="stdio", command=command, args=_string_list(raw.get("args", []), f"Server {name}.args"), env=_expand_map(_string_map(raw.get("env", {}), f"Server {name}.env"), environ, name, "env"))
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"Server {name}.url 必须是非空字符串")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Server {name}.url 必须是合法的 HTTP(S) URL")
    return MCPServerConfig(name=name, type="http", url=url, headers=_expand_map(_string_map(raw.get("headers", {}), f"Server {name}.headers"), environ, name, "headers"))


def _string_list(raw: object, path: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{path} 必须是字符串数组")
    return list(raw)


def _string_map(raw: object, path: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise ValueError(f"{path} 必须是字符串到字符串的映射")
    return dict(raw)


def _expand_map(values: dict[str, str], environ: dict[str, str], server_name: str, field_name: str) -> dict[str, str]:
    def expand(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            variable = match.group(1)
            if variable not in environ:
                raise ValueError(f"Server {server_name}.{field_name} 缺少环境变量 {variable}")
            return environ[variable]
        return ENV_PATTERN.sub(replace, value)
    return {key: expand(value) for key, value in values.items()}
