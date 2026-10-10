from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, Literal
import yaml
from lancher_code.errors import LanCherError
from lancher_code.permissions.models import PermissionRule, RuleScope, PermissionMatchKind
from lancher_code.permissions.rules import validate_match_kind


class PermissionRuleFileError(LanCherError):
    pass


class PermissionStorage:
    def __init__(
        self,
        *,
        project_rules_path: Path | None = None,
        user_rules_path: Path | None = None,
    ) -> None:
        self._project_rules_path = project_rules_path
        self._user_rules_path = user_rules_path
        self._session_rules: list[PermissionRule] = []
        self._session_rule_callbacks: list[Callable[[], None]] = []
        self._project_rules = self._load_rules(project_rules_path, "project")
        self._user_rules = self._load_rules(user_rules_path, "user")

    @property
    def project_rules_path(self) -> Path | None:
        return self._project_rules_path

    def rules_for_scope(self, scope: RuleScope) -> list[PermissionRule]:
        if scope == "session":
            return list(self._session_rules)
        if scope == "project":
            return list(self._project_rules)
        return list(self._user_rules)

    @property
    def user_rules_path(self) -> Path | None:
        return self._user_rules_path

    def replace_rules(
        self,
        scope: Literal["project", "user"],
        rules: list[PermissionRule],
        *,
        persist: bool = True,
    ) -> None:
        """替换指定持久化层的规则；会话级规则保持不变。"""
        path = self._project_rules_path if scope == "project" else self._user_rules_path
        if path is None:
            raise PermissionRuleFileError(f"{scope} 权限规则文件路径未配置。")
        normalized = [
            PermissionRule(match=rule.match, result=rule.result, scope=scope, match_kind=rule.match_kind)
            for rule in rules
        ]
        for rule in normalized:
            validate_match_kind(rule.match_kind)
        if persist:
            self._write_rules(path, normalized)
        if scope == "project":
            self._project_rules = normalized
        else:
            self._user_rules = normalized

    def add_session_rule(self, match: str, result: Literal["allow", "deny"], *, match_kind: PermissionMatchKind = "exact") -> PermissionRule:
        normalized_match = match.strip()
        if not normalized_match:
            raise ValueError("session 权限规则的 match 不能为空。")
        validate_match_kind(match_kind)
        rule = PermissionRule(match=normalized_match, result=result, scope="session", match_kind=match_kind)
        self._session_rules.append(rule)
        self._notify_session_rules_changed()
        return rule

    def replace_session_rules(
        self,
        rules: list[PermissionRule],
        *,
        notify: bool = True,
    ) -> None:
        normalized: list[PermissionRule] = []
        for rule in rules:
            match = rule.match.strip()
            if not match:
                raise ValueError("session 权限规则的 match 不能为空。")
            if rule.result not in {"allow", "deny"}:
                raise ValueError("session 权限规则的 result 只能是 allow 或 deny。")
            validate_match_kind(rule.match_kind)
            normalized.append(PermissionRule(match=match, result=rule.result, scope="session", match_kind=rule.match_kind))
        self._session_rules = normalized
        if notify:
            self._notify_session_rules_changed()

    def subscribe_session_rules_changed(self, callback: Callable[[], None]) -> None:
        if callback not in self._session_rule_callbacks:
            self._session_rule_callbacks.append(callback)

    def _notify_session_rules_changed(self) -> None:
        for callback in tuple(self._session_rule_callbacks):
            callback()

    def add_project_rule(self, match: str, result: Literal["allow", "deny"], *, match_kind: PermissionMatchKind = "exact") -> PermissionRule:
        if self._project_rules_path is None:
            raise PermissionRuleFileError("当前会话没有配置项目级权限规则文件路径。")
        validate_match_kind(match_kind)
        rule = PermissionRule(match=match, result=result, scope="project", match_kind=match_kind)
        updated = [*self._project_rules, rule]
        self._write_rules(self._project_rules_path, updated)
        self._project_rules = updated
        return rule

    @staticmethod
    def _load_rules(path: Path | None, scope: RuleScope) -> list[PermissionRule]:
        if path is None or not path.exists():
            return []
        try:
            raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PermissionRuleFileError(f"权限规则文件不是合法的 YAML: {path}") from exc
        except OSError as exc:
            raise PermissionRuleFileError(f"无法读取权限规则文件: {path}") from exc

        if raw_data is None:
            return []
        if not isinstance(raw_data, dict):
            raise PermissionRuleFileError(f"权限规则文件顶层必须是对象: {path}")

        raw_rules = raw_data.get("rules", [])
        if raw_rules is None:
            return []
        if not isinstance(raw_rules, list):
            raise PermissionRuleFileError(f"权限规则文件中的 rules 必须是数组: {path}")

        rules: list[PermissionRule] = []
        for index, item in enumerate(raw_rules, start=1):
            if not isinstance(item, dict):
                raise PermissionRuleFileError(f"权限规则第 {index} 项必须是对象: {path}")
            match = item.get("match")
            result = item.get("result")
            if not isinstance(match, str) or not match.strip():
                raise PermissionRuleFileError(f"权限规则第 {index} 项缺少合法的 match: {path}")
            if not isinstance(result, str) or result not in {"allow", "deny"}:
                raise PermissionRuleFileError(f"权限规则第 {index} 项的 result 只能是 allow 或 deny: {path}")
            match_kind = item.get("match_kind")
            if not isinstance(match_kind, str) or match_kind not in {"exact", "glob"}:
                raise PermissionRuleFileError(f"权限规则第 {index} 项必须明确指定 match_kind 为 exact 或 glob，请重新配置；原文件已保留: {path}")
            rules.append(PermissionRule(match=match.strip(), result=result, scope=scope, match_kind=match_kind))
        return rules

    @staticmethod
    def _write_rules(path: Path, rules: list[PermissionRule]) -> None:
        data = {
            "rules": [
                {
                    "match": rule.match,
                    "result": rule.result,
                    "match_kind": rule.match_kind,
                }
                for rule in rules
            ]
        }
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
