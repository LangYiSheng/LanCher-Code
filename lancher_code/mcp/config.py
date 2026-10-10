from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from lancher_code.config.paths import get_global_mcp_config_path, get_project_mcp_config_path
from lancher_code.logging_system import get_logger

logger = get_logger("mcp.config")

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(slots=True, frozen=True)
class MCPConfigIssue:
    source: str
    message: str
    server_name: str | None = None


class MCPConfigValidationError(ValueError):
    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


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
    startup_timeout_seconds: float | None = None
    tool_timeout_seconds: float = 60.0
    close_timeout_seconds: float | None = None
    # 运行值可以展开凭据，原文用于诊断、重连和配置回写，禁止向模型展示。
    env_template: dict[str, str] = field(default_factory=dict, repr=False)
    headers_template: dict[str, str] = field(default_factory=dict, repr=False)

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
            config = validate_server_config(name, raw)
            config.env_template = dict(config.env)
            config.headers_template = dict(config.headers)
            if config.enabled:
                config.env = _expand_map(config.env, env_source, config.name, "env")
                config.headers = _expand_map(config.headers, env_source, config.name, "headers")
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


def validate_server_config(name: object, raw: object) -> MCPServerConfig:
    """共享配置校验；保留凭据占位符，连接时再展开环境变量。"""
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise MCPConfigValidationError(f"MCP Server 名称不合法: {name!s}", field="name")
    if not isinstance(raw, dict):
        raise MCPConfigValidationError(f"Server {name} 配置必须是对象", field="type")
    server_type = raw.get("type")
    if server_type not in {"stdio", "http"}:
        raise MCPConfigValidationError(f"Server {name}.type 只能是 stdio 或 http", field="type")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MCPConfigValidationError(f"Server {name}.enabled 必须是布尔值", field="enabled")
    timeouts = {
        "startup_timeout_seconds": _timeout(raw, name, "startup_timeout_seconds", None),
        "tool_timeout_seconds": _timeout(raw, name, "tool_timeout_seconds", 60.0),
        "close_timeout_seconds": _timeout(raw, name, "close_timeout_seconds", None),
    }
    if server_type == "stdio":
        command = raw.get("command")
        if enabled and (not isinstance(command, str) or not command.strip()):
            raise MCPConfigValidationError(f"Server {name}.command 必须是非空字符串", field="command")
        return MCPServerConfig(name=name, type="stdio", enabled=enabled, command=command,
                               args=_string_list(raw.get("args", []), f"Server {name}.args"),
                               env=_string_map(raw.get("env", {}), f"Server {name}.env"), **timeouts)
    url = raw.get("url")
    if enabled:
        if not isinstance(url, str) or not url.strip():
            raise MCPConfigValidationError(f"Server {name}.url 必须是非空字符串", field="url")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MCPConfigValidationError(f"Server {name}.url 必须是合法的 HTTP(S) URL", field="url")
    return MCPServerConfig(name=name, type="http", enabled=enabled, url=url,
                           headers=_string_map(raw.get("headers", {}), f"Server {name}.headers"), **timeouts)


def _timeout(raw: dict[str, object], name: str, key: str, default: float | None) -> float | None:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise MCPConfigValidationError(f"Server {name}.{key} 必须是有限的正数", field=key)
    return float(value)


def _string_list(raw: object, path: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise MCPConfigValidationError(f"{path} 必须是字符串数组", field="args")
    return list(raw)


def _string_map(raw: object, path: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise MCPConfigValidationError(f"{path} 必须是字符串到字符串的映射", field=path.rsplit(".", 1)[-1])
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
