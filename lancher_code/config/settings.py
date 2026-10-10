from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from lancher_code.config.loader import load_config, load_config_data
from lancher_code.config.writer import serialize_config, write_yaml_atomic
from lancher_code.errors import ConfigError
from lancher_code.config.models import AppConfig, RuntimeConfig, UIConfig
from lancher_code.permissions.models import PermissionRule
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.mcp.config import MCPConfigValidationError, validate_server_config


class SettingsError(ValueError):
    def __init__(self, message: str, *, field_id: str | None = None) -> None:
        super().__init__(message)
        self.field_id = field_id


@dataclass(slots=True)
class SettingsSnapshot:
    config: AppConfig
    global_mcp: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_mcp: dict[str, dict[str, Any]] = field(default_factory=dict)
    project_rules: list[PermissionRule] = field(default_factory=list)
    user_rules: list[PermissionRule] = field(default_factory=list)


class SettingsService:
    """集中读取、校验和保存设置页涉及的三类配置。"""

    def __init__(
        self,
        *,
        config_path: Path,
        global_mcp_path: Path,
        project_mcp_path: Path,
        permission_storage: PermissionStorage,
    ) -> None:
        self.config_path = config_path
        self.global_mcp_path = global_mcp_path
        self.project_mcp_path = project_mcp_path
        self.permission_storage = permission_storage
        # 服务与本次应用运行同寿命；关闭设置页不会清除待重启状态。
        self.mcp_restart_required = False

    def load(self) -> SettingsSnapshot:
        try:
            config = self._read_config()
            global_mcp = self._read_mcp_layer(self.global_mcp_path)
            project_mcp = self._read_mcp_layer(self.project_mcp_path)
        except Exception as exc:
            raise SettingsError(str(exc)) from exc
        return SettingsSnapshot(
            config=config,
            global_mcp=global_mcp,
            project_mcp=project_mcp,
            project_rules=self.permission_storage.rules_for_scope("project"),
            user_rules=self.permission_storage.rules_for_scope("user"),
        )

    def save_models(self, config: AppConfig) -> AppConfig:
        """只提交模型目录；使用磁盘上的其他设置，避免覆盖无关编辑。"""
        current = self._read_config()
        current.providers = deepcopy(config.providers)
        current.default_model = config.default_model
        return self._save_config(current)

    def save_ui(self, ui: UIConfig) -> AppConfig:
        current = self._read_config()
        current.ui = deepcopy(ui)
        return self._save_config(current)

    def save_runtime(self, runtime: RuntimeConfig) -> AppConfig:
        """只保存系统运行配置，运行时应用由智能体核心负责。"""
        current = self._read_config()
        current.runtime = deepcopy(runtime)
        return self._save_config(current)

    def _read_config(self) -> AppConfig:
        try:
            return load_config(self.config_path)
        except ConfigError as exc:
            raise SettingsError(exc.user_message) from exc

    def _save_config(self, config: AppConfig) -> AppConfig:
        # 按领域保存，保留尚未编辑的其他配置项。
        raw = deepcopy(self._read_yaml(self.config_path, required=True))
        data = serialize_config(config)
        for key in ("ui", "runtime"):
            data[key] = {**(raw.get(key) or {}), **data[key]}
        raw.update(data)
        try:
            validated = load_config_data(raw)
            write_yaml_atomic(self.config_path, raw)
        except (ConfigError, OSError) as exc:
            raise SettingsError(f"无法保存设置：{exc}") from exc
        return validated

    def save_mcp(self, scope: Literal["global", "project"], servers: dict[str, dict[str, Any]]) -> None:
        if scope not in {"global", "project"}:
            raise SettingsError("未知 MCP 作用范围。")
        self._validate_mcp(servers, "全局 MCP" if scope == "global" else "项目 MCP")
        path = self.global_mcp_path if scope == "global" else self.project_mcp_path
        raw = self._read_yaml(path)
        if not isinstance(raw, dict):
            raise SettingsError("MCP 配置必须是对象。")
        changed = raw.get("mcp_servers", {}) != servers
        self._write_domain(path, {**raw, "mcp_servers": deepcopy(servers)})
        self.mcp_restart_required |= changed

    def save_rules(self, scope: Literal["project", "user"], rules: list[PermissionRule]) -> None:
        if scope not in {"project", "user"}:
            raise SettingsError("未知权限作用范围。")
        self._validate_rules(rules)
        path = self.permission_storage.project_rules_path if scope == "project" else self.permission_storage.user_rules_path
        if path is None:
            raise SettingsError("权限规则文件路径未配置。")
        self._write_domain(path, self._rules_data(rules))
        self.permission_storage.replace_rules(scope, deepcopy(rules), persist=False)

    def _write_domain(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            write_yaml_atomic(path, payload)
        except (ConfigError, OSError) as exc:
            raise SettingsError(f"无法保存设置：{exc}") from exc

    @staticmethod
    def _read_yaml(path: Path, *, required: bool = False) -> Any:
        if not path.exists():
            if required:
                raise SettingsError(f"配置文件不存在：{path}")
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise SettingsError(f"无法读取配置文件 {path}：{exc}") from exc

    def _read_mcp_layer(self, path: Path) -> dict[str, dict[str, Any]]:
        raw = self._read_yaml(path)
        if not isinstance(raw, dict) or not isinstance(raw.get("mcp_servers", {}), dict):
            raise SettingsError(f"MCP 配置格式无效：{path}")
        return {str(name): dict(value) for name, value in raw.get("mcp_servers", {}).items() if isinstance(value, dict)}

    @staticmethod
    def _validate_mcp(servers: dict[str, dict[str, Any]], label: str) -> None:
        for name, server in servers.items():
            try:
                validate_server_config(name, server)
            except MCPConfigValidationError as exc:
                field_id = {
                    "name": "mcp-name", "type": "mcp-type", "enabled": "mcp-enabled",
                    "command": "mcp-target", "url": "mcp-target", "args": "mcp-args",
                    "env": "mcp-map", "headers": "mcp-map",
                }[exc.field]
                raise SettingsError(f"{label}：{exc}", field_id=field_id) from exc

    @staticmethod
    def _validate_rules(rules: list[PermissionRule]) -> None:
        for rule in rules:
            if not rule.match.strip() or rule.result not in {"allow", "deny"} or rule.match_kind not in {"exact", "glob"}:
                raise SettingsError("权限规则必须包含 match，result 只能是 allow 或 deny。", field_id="rule-match")

    @staticmethod
    def _rules_data(rules: list[PermissionRule]) -> dict[str, Any]:
        return {"rules": [{"match": rule.match, "result": rule.result, "match_kind": rule.match_kind} for rule in rules]}
