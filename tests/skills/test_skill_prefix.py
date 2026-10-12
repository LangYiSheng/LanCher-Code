from pathlib import Path

import pytest

from lancher_code.agent.capabilities import AgentCapabilities
from lancher_code.agent.skill_context import SkillRuntime
from lancher_code.agent.skills import SkillError, SkillsService
from lancher_code.sessions.controller import SessionController
from lancher_code.tools.core.registry import ToolRegistry


def write_skill(root: Path, name='review', description='审查代码'):
    directory = root / '.lancher' / 'skills' / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\nBODY_SENTINEL', encoding='utf-8')


def test_different_projects_and_disabled_states_share_identical_catalog_prefix(tmp_path, openai_provider_config):
    user, first_project, second_project = [tmp_path / name for name in ('user', 'first', 'second')]
    write_skill(user)
    write_skill(first_project)
    second_project.mkdir()
    (first_project / 'AGENTS.md').write_text('第一项目约定', encoding='utf-8')
    (second_project / 'AGENTS.md').write_text('第二项目约定', encoding='utf-8')
    first_session = SessionController(openai_provider_config, cwd=first_project)
    second_session = SessionController(openai_provider_config, cwd=second_project)
    try:
        for session, project in ((first_session, first_project), (second_session, second_project)):
            AgentCapabilities(session, ToolRegistry(), ensure_idle=lambda: None, project_root=project,
                              skills_service=SkillsService(project, user))
            session.create_user_message('检查代码')
        second_session.context_state.disabled_skills = ['user/review']
        first = first_session.build_request([], allow_tool_calls=True)
        second = second_session.build_request([], allow_tool_calls=True)
        count = first.shared_system_count
        assert count == second.shared_system_count == 3
        assert first.system[:count] == second.system[:count]
        assert first.system[count:] != second.system[count:]
        shared = '\n'.join(first.system[:count])
        assert '<skills_catalog scope="user">' in shared and 'user/review' in shared
        assert all(value not in shared for value in (
            str(first_project), str(second_project), '第一项目约定', '第二项目约定', 'BODY_SENTINEL'))
        assert '"user/review": "project/review"' in first.messages[-1].blocks[0].text
        assert '"disabled": ["user/review"]' in second.messages[-1].blocks[0].text
        assert all('<skills_state>' not in block for block in first.system + second.system)
    finally:
        first_session.close()
        second_session.close()


def test_disabling_unloaded_global_skill_appends_state_and_enabling_clears_it(tmp_path, openai_provider_config):
    user, project = tmp_path / 'user', tmp_path / 'project'
    write_skill(user)
    project.mkdir()
    session = SessionController(openai_provider_config, cwd=project)
    runtime = SkillRuntime(SkillsService(project, user), session)
    session.bind_agent_context(runtime.context_blocks)
    try:
        session.create_user_message('检查代码')
        first = session.build_request([], allow_tool_calls=True)
        runtime.set_enabled('user/review', False)
        disabled = session.build_request([], allow_tool_calls=True)
        assert disabled.system == first.system
        assert disabled.messages[:len(first.messages)] == first.messages
        assert '"disabled": ["user/review"]' in disabled.messages[-1].blocks[0].text
        runtime.set_enabled('user/review', True)
        enabled = session.build_request([], allow_tool_calls=True)
        assert enabled.system == first.system
        assert enabled.messages[:len(disabled.messages)] == disabled.messages
        assert '"disabled": []' in enabled.messages[-1].blocks[0].text
    finally:
        session.close()


def test_catalog_budget_reserves_project_space_without_changing_shared_catalog(tmp_path, openai_provider_config):
    user, project = tmp_path / 'user', tmp_path / 'project'
    for index in range(80):
        write_skill(user, f'global-{index:03d}', '定位代码并检查约束。' * 15)
        write_skill(project, f'local-{index:03d}', '验证项目实现与约定。' * 15)
    service = SkillsService(project, user)
    session = SessionController(openai_provider_config, cwd=project)
    runtime = SkillRuntime(service, session)
    try:
        blocks = runtime.context_blocks(session.context_state)
        catalogs = [block for block in blocks if block.startswith('<skills_catalog ')]
        assert len(catalogs) == 2
        assert len(catalogs[0]) <= 4000 and sum(map(len, catalogs)) <= 8000
        assert 'global-000' in catalogs[0] and 'local-000' in catalogs[1]
        assert all('部分技能未列出' in catalog for catalog in catalogs)
        write_skill(project, 'aaa-new')
        service.reload()
        updated = runtime.context_blocks(session.context_state)
        assert updated[0] == catalogs[0]
        assert updated[1] != catalogs[1]
    finally:
        session.close()


def test_manual_project_override_is_explicit_in_state_and_cannot_auto_load(tmp_path, openai_provider_config):
    user, project = tmp_path / 'user', tmp_path / 'project'
    write_skill(user)
    write_skill(project)
    (project / '.lancher' / 'skills' / 'review' / 'SKILL.md').write_text(
        '---\nname: review\ndescription: 项目审查\ndisable-model-invocation: true\n---\n手动指南', encoding='utf-8')
    service = SkillsService(project, user)
    session = SessionController(openai_provider_config, cwd=project)
    runtime = SkillRuntime(service, session)
    try:
        blocks = runtime.context_blocks(session.context_state)
        assert 'user/review' in blocks[0]
        assert '"explicit_only": ["project/review"]' in blocks[-1]
        assert '"user/review": "project/review"' in blocks[-1]
        assert all('<skills_catalog scope="project">' not in block for block in blocks)
        with pytest.raises(SkillError, match='需要用户'):
            runtime.activate(service.load('review'))
        assert runtime.apply_explicit('$review') == ['project/review']
        assert session.context_state.skill_activations['project/review']['loaded'] is True
    finally:
        session.close()
