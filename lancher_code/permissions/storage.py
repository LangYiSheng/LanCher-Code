from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, Literal

import yaml

from lancher_code.errors import LanCherError
from lancher_code.permissions.models import DirectoryGrant, PermissionMatchKind, PermissionRule, RuleScope
from lancher_code.permissions.rules import validate_match_kind


RULE_FORMAT_VERSION = 2


def validate_command_context(cwd: object, shell: object) -> None:
    """拒绝规则可选地限制实际 Shell 和工作目录。"""
    if cwd is None and shell is None:
        return
    if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd or not Path(cwd).is_absolute():
        raise ValueError("command_cwd 必须是绝对路径。")
    if not isinstance(shell, str) or shell not in {"powershell", "sh", "bash"}:
        raise ValueError("command_shell 必须为 powershell、sh 或 bash。")


def validate_directory_grant(grant: DirectoryGrant) -> DirectoryGrant:
    """保存确定的绝对路径，不在加载时再次跟随可能变化的链接。"""
    if not isinstance(grant.path, str) or not grant.path.strip() or "\x00" in grant.path or not Path(grant.path).is_absolute():
        raise ValueError("目录授权 path 必须是非空绝对路径。")
    if not isinstance(grant.access, str) or grant.access not in {"read", "write"}:
        raise ValueError("目录授权 access 必须为 read 或 write。")
    if not isinstance(grant.recursive, bool):
        raise ValueError("目录授权 recursive 必须为布尔值。")
    return DirectoryGrant(os.path.abspath(grant.path), grant.access, grant.recursive)


class PermissionRuleFileError(LanCherError):
    pass


class PermissionStorage:
    def __init__(self, *, project_rules_path: Path | None = None, user_rules_path: Path | None = None) -> None:
        self._project_rules_path = project_rules_path
        self._user_rules_path = user_rules_path
        self._session_rules: list[PermissionRule] = []
        self._session_grants: list[DirectoryGrant] = []
        self._session_rule_callbacks: list[Callable[[], None]] = []
        self._project_rules = self._load_rules(project_rules_path, "project")
        self._user_rules = self._load_rules(user_rules_path, "user")

    @property
    def project_rules_path(self) -> Path | None:
        return self._project_rules_path

    @property
    def user_rules_path(self) -> Path | None:
        return self._user_rules_path

    @property
    def session_grants(self) -> list[DirectoryGrant]:
        return list(self._session_grants)

    def rules_for_scope(self, scope: RuleScope) -> list[PermissionRule]:
        if scope == "session":
            return list(self._session_rules)
        if scope == "project":
            return list(self._project_rules)
        return list(self._user_rules)

    @staticmethod
    def _normalize_rule(rule: PermissionRule, scope: RuleScope) -> PermissionRule:
        if not isinstance(rule.match, str) or not rule.match.strip():
            raise ValueError("权限拒绝规则的 match 不能为空。")
        if rule.result != "deny":
            raise ValueError("权限规则只支持 deny；旧 allow 授权不再支持。")
        validate_match_kind(rule.match_kind)
        validate_command_context(rule.command_cwd, rule.command_shell)
        return PermissionRule(rule.match.strip(), "deny", scope, rule.match_kind, rule.command_cwd, rule.command_shell)

    def replace_rules(self, scope: Literal["project", "user"], rules: list[PermissionRule], *, persist: bool = True) -> None:
        path = self._project_rules_path if scope == "project" else self._user_rules_path
        if path is None:
            raise PermissionRuleFileError(f"{scope} 权限规则文件路径未配置。")
        normalized = [self._normalize_rule(rule, scope) for rule in rules]
        if persist:
            self._write_rules(path, normalized)
        if scope == "project":
            self._project_rules = normalized
        else:
            self._user_rules = normalized

    def add_session_rule(self, match: str, result: Literal["deny"], *, match_kind: PermissionMatchKind = "exact",
                         command_cwd: str | None = None, command_shell: str | None = None) -> PermissionRule:
        rule = self._normalize_rule(PermissionRule(match, result, "session", match_kind, command_cwd, command_shell), "session")
        self._session_rules.append(rule)
        self._notify_session_rules_changed()
        return rule

    def replace_session_rules(self, rules: list[PermissionRule], *, notify: bool = True) -> None:
        self._session_rules = [self._normalize_rule(rule, "session") for rule in rules]
        if notify:
            self._notify_session_rules_changed()

    def replace_session_grants(self, grants: list[DirectoryGrant], *, notify: bool = True) -> None:
        self._session_grants = list(dict.fromkeys(validate_directory_grant(grant) for grant in grants))
        if notify:
            self._notify_session_rules_changed()

    def add_directory_grants(self, grants: list[DirectoryGrant]) -> None:
        normalized = list(dict.fromkeys([*self._session_grants, *(validate_directory_grant(grant) for grant in grants)]))
        if normalized != self._session_grants:
            self._session_grants = normalized
            self._notify_session_rules_changed()

    def subscribe_session_rules_changed(self, callback: Callable[[], None]) -> None:
        if callback not in self._session_rule_callbacks:
            self._session_rule_callbacks.append(callback)

    def _notify_session_rules_changed(self) -> None:
        for callback in tuple(self._session_rule_callbacks):
            callback()

    def add_project_rule(self, match: str, result: Literal["deny"], *, match_kind: PermissionMatchKind = "exact",
                         command_cwd: str | None = None, command_shell: str | None = None) -> PermissionRule:
        rule = self._normalize_rule(PermissionRule(match, result, "project", match_kind, command_cwd, command_shell), "project")
        self.replace_rules("project", [*self._project_rules, rule])
        return rule

    @classmethod
    def _load_rules(cls, path: Path | None, scope: RuleScope) -> list[PermissionRule]:
        if path is None or not path.exists():
            return []
        try:
            raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PermissionRuleFileError(f"权限规则文件不是合法的 YAML，原文件已保留: {path}") from exc
        except OSError as exc:
            raise PermissionRuleFileError(f"无法读取权限规则文件: {path}") from exc
        if not isinstance(raw_data, dict) or type(raw_data.get("format_version")) is not int or raw_data["format_version"] != RULE_FORMAT_VERSION:
            raise PermissionRuleFileError(f"权限规则文件必须为 format_version: {RULE_FORMAT_VERSION}；旧格式不再支持，原文件已保留: {path}")
        raw_rules = raw_data.get("rules")
        if not isinstance(raw_rules, list):
            raise PermissionRuleFileError(f"权限规则文件中的 rules 必须是数组，原文件已保留: {path}")
        rules: list[PermissionRule] = []
        for index, item in enumerate(raw_rules, start=1):
            if not isinstance(item, dict):
                raise PermissionRuleFileError(f"权限规则第 {index} 项必须是对象: {path}")
            try:
                rule = PermissionRule(item.get("match"), item.get("result"), scope, item.get("match_kind"),
                                      item.get("command_cwd"), item.get("command_shell"))
                rules.append(cls._normalize_rule(rule, scope))
            except (ValueError, TypeError) as exc:
                raise PermissionRuleFileError(f"权限规则第 {index} 项无效，原文件已保留: {path}: {exc}") from exc
        return rules

    @staticmethod
    def _write_rules(path: Path, rules: list[PermissionRule]) -> None:
        data = {"format_version": RULE_FORMAT_VERSION, "rules": [
            {"match": rule.match, "result": rule.result, "match_kind": rule.match_kind,
             **({"command_cwd": rule.command_cwd, "command_shell": rule.command_shell} if rule.command_cwd is not None else {})}
            for rule in rules
        ]}
        yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(yaml_text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise PermissionRuleFileError(f"无法保存权限规则文件：{path}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
