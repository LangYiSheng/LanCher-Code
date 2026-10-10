"""权限规则的顺序、匹配方式与独立保存。"""
from __future__ import annotations
from copy import deepcopy
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Select, Static
from lancher_code.config.settings import SettingsService
from lancher_code.permissions.models import PermissionRule
from lancher_code.tui.settings.common import SettingsDomainEditor, DomainSaved, field

class PermissionSettingsEditor(SettingsDomainEditor):
    domain = "permissions"
    editor_selector = "#rule-editor"

    def __init__(self, service: SettingsService) -> None:
        super().__init__(service, id="page-permissions")
        self._rule_scope = "project"
        self._editing_rule_index: int | None = None

    def on_mount(self) -> None:
        self.query_one("#rules-table", DataTable).add_columns("顺序", "匹配表达式", "匹配方式", "结果")

    def show_catalog(self) -> None:
        self.editing = False
        self.query_one("#rules-list").display = True
        self.query_one("#rule-editor").display = False
        self._refresh_rules_table()

    def save(self) -> None:
        rules = deepcopy(self._rules())
        rule = PermissionRule(self.query_one("#rule-match", Input).value.strip(),
            self.query_one("#rule-result", Select).value, self._rule_scope,
            match_kind=self.query_one("#rule-match-kind", Select).value)
        if self._editing_rule_index is None:
            rules.append(rule)
        else:
            rules[self._editing_rule_index] = rule
        self._commit_rules(rules)

    def compose(self) -> ComposeResult:
        with Vertical(id="rules-list", classes="list-region"):
            yield Select((("当前项目", "project"), ("全局", "user")), value="project", allow_blank=False, id="rule-scope")
            yield Static("会话规则优先于项目，项目优先于全局；同层最后匹配的规则生效。", classes="scope-note")
            yield DataTable(id="rules-table", cursor_type="row")
            yield Button("＋ 添加权限规则", id="rule-new", classes="link-button")
        with Vertical(id="rule-editor", classes="form"):
            yield Static("权限规则", classes="editor-title")
            yield Static("", id="rule-editor-scope", classes="scope-note")
            yield from field("匹配表达式", Input(placeholder="例如 RunCommand(git status)", id="rule-match"))
            yield from field("匹配方式", Select((("精确匹配", "exact"), ("通配规则（glob）", "glob")), value="exact", allow_blank=False, id="rule-match-kind"))
            yield Static("精确匹配只授权完整目标；通配规则可覆盖多个目标。", classes="scope-note")
            yield from field("处理方式", Select((("允许", "allow"), ("拒绝", "deny")), value="allow", allow_blank=False, id="rule-result"))
            with Horizontal(classes="row-actions"):
                yield Button("上移", id="rule-up")
                yield Button("下移", id="rule-down")
                yield Button("删除", id="rule-delete", classes="danger-button")

    @on(Select.Changed, "#rule-scope")
    def rule_scope_changed(self, event: Select.Changed) -> None:
        if event.value in {"project", "user"}:
            self._rule_scope = event.value; self._refresh_rules_table()

    def _rules(self) -> list[PermissionRule]:
        return self.snapshot.project_rules if self._rule_scope == "project" else self.snapshot.user_rules

    def _refresh_rules_table(self) -> None:
        table = self.query_one("#rules-table", DataTable); table.clear()
        if self.snapshot:
            for i, rule in enumerate(self._rules()):
                table.add_row(str(i + 1), Text(rule.match), {"exact":"精确", "glob":"通配"}[rule.match_kind], "允许" if rule.result == "allow" else "拒绝", key=str(i))

    @on(DataTable.RowSelected, "#rules-table")
    def select_rule(self, event: DataTable.RowSelected) -> None:
        self.edit_rule(int(str(event.row_key.value)))

    @on(Button.Pressed, "#rule-new")
    def new_rule(self) -> None:
        self.edit_rule()

    def edit_rule(self, index: int | None = None) -> None:
        self._editing_rule_index = index
        rule = self._rules()[index] if index is not None else PermissionRule("", "allow", self._rule_scope, match_kind="exact")
        self.query_one("#rules-list").display = False
        self.query_one("#rule-editor").display = True
        self.query_one("#rule-editor-scope", Static).update(("当前项目" if self._rule_scope == "project" else "全局") + " · 保存后立即生效")
        self.query_one("#rule-match", Input).value = rule.match
        self.query_one("#rule-result", Select).value = rule.result
        self.query_one("#rule-match-kind", Select).value = rule.match_kind
        for action in ("up", "down", "delete"):
            self.query_one(f"#rule-{action}").display = index is not None
        self.begin_form("#rule-match")

    def _commit_rules(self, rules: list[PermissionRule]) -> None:
        self.service.save_rules(self._rule_scope, rules)
        if self._rule_scope == "project": self.snapshot.project_rules = rules
        else: self.snapshot.user_rules = rules
        self.show_catalog()
        self.post_message(DomainSaved("permissions", "权限规则已保存。"))

    @on(Button.Pressed, "#rule-up")
    @on(Button.Pressed, "#rule-down")
    @on(Button.Pressed, "#rule-delete")
    def rule_action(self, event: Button.Pressed) -> None:
        index = self._editing_rule_index
        if index is None: return
        action = event.button.id.removeprefix("rule-")
        def commit() -> None:
            rules = deepcopy(self._rules())
            if action == "delete": rules.pop(index)
            else:
                target = index + (-1 if action == "up" else 1)
                if not 0 <= target < len(rules): return
                rules[index], rules[target] = rules[target], rules[index]
            self._commit_rules(rules)
        self.guard(lambda: self.commit(commit))
