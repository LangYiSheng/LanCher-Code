from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from lancher_code.config_system.loader import load_config_data
from lancher_code.config_system.writer import serialize_config
from lancher_code.models import AppConfig, PermissionRule
from lancher_code.permission_engine import PermissionStorage


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

    def load(self) -> SettingsSnapshot:
        try:
            config = load_config_data(self._read_yaml(self.config_path, required=True))
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

    def save(self, snapshot: SettingsSnapshot) -> None:
        config_data = serialize_config(snapshot.config)
        # 复用正式 loader 做完整模型配置校验。
        load_config_data(config_data)
        self._validate_mcp(snapshot.global_mcp, "全局 MCP")
        self._validate_mcp(snapshot.project_mcp, "项目 MCP")
        self._validate_rules(snapshot.project_rules)
        self._validate_rules(snapshot.user_rules)

        payloads = {
            self.config_path: config_data,
            self.global_mcp_path: {"mcp_servers": snapshot.global_mcp},
            self.project_mcp_path: {"mcp_servers": snapshot.project_mcp},
        }
        project_rules_path = self.permission_storage.project_rules_path
        user_rules_path = self.permission_storage.user_rules_path
        if project_rules_path is None or user_rules_path is None:
            raise SettingsError("权限规则文件路径未配置。")
        payloads[project_rules_path] = self._rules_data(snapshot.project_rules)
        payloads[user_rules_path] = self._rules_data(snapshot.user_rules)
        self._atomic_write_many(payloads)
        # 全部文件成功落盘后才切换当前会话使用的规则。
        self.permission_storage.replace_rules("project", snapshot.project_rules, persist=False)
        self.permission_storage.replace_rules("user", snapshot.user_rules, persist=False)

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
            if not name or not all(char.isalnum() or char in "_-" for char in name):
                raise SettingsError(f"{label} 服务器名称不合法：{name!r}", field_id="mcp-name")
            kind = server.get("type")
            if kind not in {"stdio", "http"}:
                raise SettingsError(f"Server {name} 的类型只能是 stdio 或 http。", field_id="mcp-type")
            if not isinstance(server.get("enabled", True), bool):
                raise SettingsError(f"Server {name}.enabled 必须是布尔值。")
            required = "command" if kind == "stdio" else "url"
            if server.get("enabled", True) and not str(server.get(required, "")).strip():
                raise SettingsError(f"Server {name}.{required} 不能为空。", field_id="mcp-target")

    @staticmethod
    def _validate_rules(rules: list[PermissionRule]) -> None:
        for rule in rules:
            if not rule.match.strip() or rule.result not in {"allow", "deny"}:
                raise SettingsError("权限规则必须包含 match，result 只能是 allow 或 deny。", field_id="rule-match")

    @staticmethod
    def _rules_data(rules: list[PermissionRule]) -> dict[str, Any]:
        return {"rules": [{"match": rule.match, "result": rule.result} for rule in rules]}

    @staticmethod
    def _atomic_write_many(payloads: dict[Path, Any]) -> None:
        staged: dict[Path, Path] = {}
        try:
            for path, data in payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
                temp_path = Path(temp_name)
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                staged[path] = temp_path
            for path, temp_path in staged.items():
                os.replace(temp_path, path)
        finally:
            for temp_path in staged.values():
                temp_path.unlink(missing_ok=True)
