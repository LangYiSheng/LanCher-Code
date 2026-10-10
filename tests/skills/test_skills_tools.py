from __future__ import annotations

from pathlib import Path

import pytest

from lancher_code.agent.skills import SkillsService
from lancher_code.tools.builtin.skills import LoadSkillTool, ReadSkillResourceTool
from lancher_code.tools.context import ToolContext


def setup_skill(tmp_path: Path) -> SkillsService:
    directory = tmp_path / ".lancher" / "skills" / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: review\ndescription: 审查代码\n---\n不应写入工具历史的正文。", encoding="utf-8")
    (directory / "notes.txt").write_text("引用资料\n下一行", encoding="utf-8")
    return SkillsService(tmp_path, tmp_path / "unused")


@pytest.mark.asyncio
async def test_load_projects_body_through_callback_and_keeps_tool_history_small(tmp_path: Path) -> None:
    service = setup_skill(tmp_path)
    snapshots = []
    tool = LoadSkillTool(service, snapshots.append)
    context = ToolContext(cwd=tmp_path, timeout_seconds=1, work_phase="plan")
    result = await tool.execute({"skill": "review"}, context)

    assert not result.is_error
    assert snapshots[0].body == "不应写入工具历史的正文。"
    assert "正文" not in result.content
    assert "body" not in result.metadata
    assert result.metadata["skill_id"] == "project/review"
    assert tool.definition.is_system_tool
    assert tool.definition.allowed_phases == ("discuss", "plan", "execute")
    claim, = tool.resource_claims({"skill": "review"}, context)
    assert claim.kind == "path" and claim.mode == "shared" and claim.lifetime == "invocation"
    assert claim.key == str(Path(service.resolve("review").path).resolve())


@pytest.mark.asyncio
async def test_disable_callback_blocks_load_and_resource_before_reading(tmp_path: Path) -> None:
    service = setup_skill(tmp_path)
    snapshots = []
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    for tool, arguments in (
        (LoadSkillTool(service, snapshots.append, lambda _: False), {"skill": "review"}),
        (ReadSkillResourceTool(service, lambda _: False), {"skill": "review", "relative_path": "notes.txt"}),
    ):
        result = await tool.execute(arguments, context)
        assert result.is_error and result.error_code == "skill_disabled"
    assert not snapshots


@pytest.mark.asyncio
async def test_resource_tool_pages_text_and_declares_exact_read_resource(tmp_path: Path) -> None:
    service = setup_skill(tmp_path)
    tool = ReadSkillResourceTool(service)
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    arguments = {"skill": "review", "relative_path": "notes.txt", "line_count": 1}
    result = await tool.execute(arguments, context)

    assert not result.is_error
    assert "1\t引用资料" in result.content
    assert "下一行" not in result.content
    assert result.metadata["next_line"] == 2
    claim, = tool.resource_claims(arguments, context)
    assert claim.key == str(service.resource_path("review", "notes.txt").resolve())
    assert claim.mode == "shared"


@pytest.mark.asyncio
async def test_tool_invalid_arguments_and_traversal_do_not_call_loader(tmp_path: Path) -> None:
    service = setup_skill(tmp_path)
    snapshots = []
    context = ToolContext(cwd=tmp_path, timeout_seconds=1)
    load = LoadSkillTool(service, snapshots.append)
    assert (await load.execute({"skill": 5}, context)).error_code == "invalid_arguments"
    resource = ReadSkillResourceTool(service)
    arguments = {"skill": "review", "relative_path": "../secret.txt"}
    assert resource.resource_claims(arguments, context) == ()
    assert (await resource.execute(arguments, context)).error_code == "skill_resource_outside_root"
    assert not snapshots


@pytest.mark.asyncio
async def test_resource_tool_keeps_empty_lines_in_numbered_page(tmp_path: Path) -> None:
    service = setup_skill(tmp_path)
    service.resource_path("review", "blank.txt").write_text("first\n\nlast", encoding="utf-8")
    tool = ReadSkillResourceTool(service)
    result = await tool.execute(
        {"skill": "review", "relative_path": "blank.txt", "line_count": 2},
        ToolContext(cwd=tmp_path, timeout_seconds=1),
    )
    assert "1\tfirst\n2\t\n" in result.content
    assert result.metadata["next_line"] == 3
