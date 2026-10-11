from pathlib import Path

import pytest
from textual.widgets import Button, Static
from textual.containers import VerticalScroll

from lancher_code.tui.chat_controls import ExecutionAccessScreen
from lancher_code.tui.permission import InlinePermissionPanel, PermissionScopesScreen
from lancher_code.tools.builtin.read_file import ReadFileTool
from test_permission_analysis import _chat_app, _export, _request, _visible


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [(48, 24), (32, 16)])
async def test_access_selection_preserves_full_while_plan_hud_is_readonly(
    openai_provider_config, ui_config, tmp_path: Path, size,
):
    app, session = _chat_app(openai_provider_config, ui_config, tmp_path)
    try:
        async with app.run_test(size=size) as pilot:
            session.set_work_phase('plan')
            app._refresh_status_bar()
            app.push_screen(ExecutionAccessScreen(), app._handle_access_selected)
            await pilot.pause()
            assert not app.screen.query('#access-default')
            full = app.screen.query_one('#access-full', Button)
            full.focus()
            await pilot.pause()
            assert _visible(full, app)
            _export(app, tmp_path, f'access-selection-{size[0]}x{size[1]}')
            await pilot.press('enter')
            await pilot.pause()
            assert session.execution_access == 'full'
            assert '只读' in app._hud.status_left_text()
            _export(app, tmp_path, f'access-plan-readonly-{size[0]}x{size[1]}')
            session.set_work_phase('execute')
            app._refresh_status_bar()
            assert '完全访问' in app._hud.status_left_text()
            _export(app, tmp_path, f'access-execute-full-{size[0]}x{size[1]}')
    finally:
        session.close()


@pytest.mark.asyncio
async def test_compact_external_read_shows_complete_session_scope(
    openai_provider_config, ui_config, tmp_path: Path,
):
    project = tmp_path / 'project'
    external = tmp_path / 'external-directory-with-a-long-name'
    project.mkdir()
    external.mkdir()
    target = external / 'evidence.txt'
    target.write_text('evidence', encoding='utf-8')
    request = _request(project, ReadFileTool(), {'path': str(target)})
    assert request.directory_grants[0].access == 'read'
    app, session = _chat_app(openai_provider_config, ui_config, project)
    try:
        async with app.run_test(size=(32, 16)) as pilot:
            await app._request_inline_permission(request)
            await pilot.pause()
            panel = app.query_one(InlinePermissionPanel)
            assert panel.has_class('-compact')
            await pilot.press('m')
            await pilot.pause()
            assert isinstance(app.screen, PermissionScopesScreen)
            text = app.screen.query_one('#scope-session-grants', Static).render().plain
            assert str(external.resolve()) in text
            assert '只读' in text and '包含子目录' in text
            assert not app.screen.query('#scope-project')
            _export(app, project, 'access-external-read-scope-32x16')
            button = app.screen.query_one('#scope-session', Button)
            app.screen.query_one('#permission-scopes-content', VerticalScroll).scroll_end(animate=False)
            button.focus()
            await pilot.pause()
            assert _visible(button, app)
            _export(app, project, 'access-external-read-scope-tail-32x16')
    finally:
        session.close()
