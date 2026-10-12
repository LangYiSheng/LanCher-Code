from __future__ import annotations
from lancher_code.agent.service import AgentService

import os
from pathlib import Path
from xml.etree import ElementTree

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Select, Static

from lancher_code.agent.runner import TurnRunner
from lancher_code.config.settings import SettingsService
from lancher_code.contracts.tools import ToolCall
from lancher_code.permissions.engine import PermissionEngine
from lancher_code.permissions.models import PermissionRule
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.sessions.controller import SessionController
from lancher_code.tools import create_default_tool_registry
from lancher_code.tools.builtin.command import RunCommandTool
from lancher_code.tools.builtin.delete_file import DeleteFileTool
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tui.app import LanCherTextualApp
from lancher_code.tui.permission import InlinePermissionPanel, PermissionDetailsScreen, PermissionOption
from lancher_code.tui.settings.permissions import PermissionSettingsEditor
from lancher_code.tui.settings.screen import SettingsScreen


class _IdleProvider:
    async def stream_chat(self, request):
        # 页面验证不发送模型请求。
        if False:
            yield None


def _chat_app(config, ui, project: Path):
    session = SessionController(config, cwd=project)
    registry = create_default_tool_registry()
    runner = TurnRunner(_IdleProvider(), session, registry, ToolExecutor(registry, cwd=project))
    return LanCherTextualApp(AgentService(runner, session), ui), session


def _request(project: Path, tool, arguments):
    if tool.definition.name == "run_command":
        arguments = {**arguments, "cwd": str(project.parent)}
    check = PermissionEngine().evaluate(
        call=ToolCall(0, "ui-permission", tool.definition.name, arguments, "{}"),
        tool=tool.definition, context=ToolContext(cwd=project, timeout_seconds=2),
    )
    assert check.decision == "ask" and check.request is not None
    return check.request


def _export(app: App, project: Path, name: str) -> None:
    # 人工预览输出可指定到项目外，普通测试默认使用自己的临时目录。
    directory = Path(os.environ.get("LANCHER_PERMISSION_ARTIFACT_DIR", str(project)))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.svg"
    path.write_text(app.export_screenshot(title=name), encoding="utf-8")
    assert ElementTree.parse(path).getroot().tag.endswith("svg")


def _visible(widget, app: App) -> bool:
    region = widget.region
    return bool(region.width and region.height and region.x >= 0 and region.y >= 0
                and region.right <= app.size.width and region.bottom <= app.size.height)


@pytest.mark.asyncio
async def test_long_command_folds_display_but_details_retain_complete_original(openai_provider_config, ui_config, tmp_path: Path) -> None:
    command = 'Write-Output "' + "ordinary " * 600 + '"\nWrite-Output TAIL_SENTINEL'
    request = _request(tmp_path, RunCommandTool(), {"command": command, "description": "长命令审批预览"})
    app, session = _chat_app(openai_provider_config, ui_config, tmp_path)
    try:
        async with app.run_test(size=(100, 40)) as pilot:
            await app._request_inline_permission(request)
            await pilot.pause()
            panel = app.query_one(InlinePermissionPanel)
            visible = panel.query_one("#permission-command", Static).render().plain
            notice = panel.query_one("#permission-command-notice", Static)
            assert "按 D 查看完整原文" in notice.render().plain
            assert _visible(notice, app)
            assert "TAIL_SENTINEL" not in visible
            assert len(visible) <= 400
            assert request.command == command
            assert panel.query_one("#permission-analysis", Static).render().plain
            panel.scroll_home(animate=False)
            await pilot.pause()
            _export(app, tmp_path, "permission-command-folded")

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, PermissionDetailsScreen)
            assert command in app.screen.text
            assert app.screen.text.count(command) == 1
            details = app.screen.query_one("#read-only-scroll", VerticalScroll)
            details.scroll_end(animate=False)
            await pilot.pause()
            assert _visible(app.screen.query_one("#read-only-close", Button), app)
            _export(app, tmp_path, "permission-command-details-tail")
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(48, 28), (60, 24)])
async def test_folded_command_notice_stays_visible_when_navigating_small_screen(openai_provider_config, ui_config, tmp_path: Path, width: int, height: int) -> None:
    command = 'Write-Output "' + "ordinary " * 600 + '"\nWrite-Output TAIL_SENTINEL'
    request = _request(tmp_path, RunCommandTool(), {"command": command, "description": "小屏长命令审批"})
    app, session = _chat_app(openai_provider_config, ui_config, tmp_path)
    try:
        async with app.run_test(size=(width, height)) as pilot:
            await app._request_inline_permission(request)
            await pilot.pause()
            panel = app.query_one(InlinePermissionPanel)
            notice = panel.query_one("#permission-command-notice", Static)
            assert notice.display and _visible(notice, app)
            await pilot.press("down")
            await pilot.pause()
            assert _visible(notice, app)
            active = next(option for option in panel.query(PermissionOption) if option.has_class("-active"))
            assert active.outcome == "deny" and _visible(active, app)
            _export(app, tmp_path, f"permission-command-folded-{width}x{height}")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, PermissionDetailsScreen)
            assert app.screen.text.count(command) == 1
            assert "TAIL_SENTINEL" in app.screen.text
    finally:
        session.close()


@pytest.mark.asyncio
async def test_short_terminal_keeps_complete_command_details_reachable(openai_provider_config, ui_config, tmp_path: Path) -> None:
    command = 'Write-Output "' + "ordinary " * 600 + '"\nWrite-Output TAIL_SENTINEL'
    request = _request(tmp_path, RunCommandTool(), {"command": command, "description": "短终端长命令审批"})
    app, session = _chat_app(openai_provider_config, ui_config, tmp_path)
    try:
        async with app.run_test(size=(32, 14)) as pilot:
            await app._request_inline_permission(request)
            await pilot.pause()
            panel = app.query_one(InlinePermissionPanel)
            assert panel.has_class("-compact")
            assert not panel.query_one("#permission-command-notice", Static).display
            assert _visible(panel.query_one("#permission-show-details", Button), app)
            assert all(_visible(option, app) for option in panel._options())
            _export(app, tmp_path, "permission-command-compact-32x14")
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, PermissionDetailsScreen)
            assert app.screen.text.count(command) == 1
            assert _visible(app.screen.query_one("#read-only-close", Button), app)
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(32, 14), (48, 20)])
async def test_narrow_recursive_delete_has_only_reachable_once_approval(openai_provider_config, ui_config, tmp_path: Path, width: int, height: int) -> None:
    target = tmp_path.parent / (tmp_path.name + "-temporary")
    target.mkdir()
    for name in ("first.txt", "last.txt"):
        (target / name).write_text(name, encoding="utf-8")
    request = _request(tmp_path, DeleteFileTool(), {"path": str(target), "recursive": True})
    app, session = _chat_app(openai_provider_config, ui_config, tmp_path)
    outcomes = []
    app._agent.resolve_permission_request = lambda resolution: outcomes.append(resolution.outcome) or True
    try:
        async with app.run_test(size=(width, height)) as pilot:
            await app._request_inline_permission(request)
            await pilot.pause()
            panel = app.query_one(InlinePermissionPanel)
            options = list(panel.query(PermissionOption))
            assert panel.has_class("-compact")
            assert [option.outcome for option in options] == ["allow_once", "deny", "allow_session"]
            assert all(_visible(option, app) for option in panel._options())
            assert _visible(panel.query_one("#permission-show-details", Button), app)
            await pilot.press("m")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            assert target.parent.as_posix() in app.screen.query_one("#scope-session-grants", Static).render().plain.replace("\\", "/")
            await pilot.press("escape")
            _export(app, tmp_path, f"permission-delete-once-{width}x{height}")

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, PermissionDetailsScreen)
            assert "first.txt" in app.screen.text and "last.txt" in app.screen.text
            assert _visible(app.screen.query_one("#read-only-close", Button), app)
            await pilot.press("escape", "down", "up", "enter")
            await pilot.pause()
            assert outcomes == ["allow_once"]
            assert list(app.query(InlinePermissionPanel)) == []
            assert (target / "first.txt").exists()
    finally:
        session.close()


def _settings_service(project: Path) -> SettingsService:
    path = project / "home" / "lancher.yaml"
    path.parent.mkdir()
    path.write_text(
        "providers:\n  test:\n    name: 测试供应商\n    protocol: openai\n    base_url: https://example.test/v1\n"
        "    api_key: test-key\n    models:\n      default:\n        model_name: gpt-test\ndefault_model: test/default\n",
        encoding="utf-8",
    )
    return SettingsService(
        config_path=path, global_mcp_path=path.parent / "mcp.yaml",
        project_mcp_path=project / ".lancher" / "mcp.yaml",
        permission_storage=PermissionStorage(
            project_rules_path=project / ".lancher" / "permissions.yaml", user_rules_path=path.parent / "permissions.yaml",
        ),
    )


@pytest.mark.asyncio
async def test_permission_settings_save_and_reorder_preserve_shell_context(tmp_path: Path) -> None:
    service = _settings_service(tmp_path)
    rule = PermissionRule(
        "RunCommand(git status)", "deny", "project", "exact",
        command_cwd=str(tmp_path.resolve()), command_shell="powershell",
    )
    service.save_rules("project", [PermissionRule("ReadFile(*)", "deny", "project", "glob"), rule])

    class SettingsApp(App):
        def on_mount(self):
            self.push_screen(SettingsScreen(service))

    app = SettingsApp()
    async with app.run_test(size=(100, 40)) as pilot:
        screen = app.screen
        screen._show_tab("permissions")
        editor = screen.query_one(PermissionSettingsEditor)
        editor.edit_rule(1)
        await pilot.pause()
        assert screen.query_one("#rule-command-cwd", Input).value == rule.command_cwd
        assert screen.query_one("#rule-command-shell", Input).value == "powershell"
        assert _visible(screen.query_one("#rule-command-cwd", Input), app)
        assert _visible(screen.query_one("#rule-command-shell", Input), app)
        _export(app, tmp_path, "permission-settings-command-context")
        screen.query_one("#rule-result", Select).value = "deny"
        await pilot.press("ctrl+s")
        await pilot.pause()
        restored = PermissionStorage(project_rules_path=service.permission_storage.project_rules_path)
        saved = restored.rules_for_scope("project")[1]
        assert saved.result == "deny" and saved.command_cwd == rule.command_cwd and saved.command_shell == "powershell"

        editor.edit_rule(1)
        editor.rule_action(Button.Pressed(screen.query_one("#rule-up", Button)))
        await pilot.pause()
        restored = PermissionStorage(project_rules_path=service.permission_storage.project_rules_path)
        assert restored.rules_for_scope("project")[0] == saved
