from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lancher_code.agent.skills import SkillError, SkillSnapshot, SkillsService
from lancher_code.agent.skills.service import MAX_FRONTMATTER_CHARS, MAX_RESOURCE_CHARS, MAX_SKILL_BODY_CHARS, MAX_SKILL_BYTES


def write_skill(base: Path, name: str = "review", *, body: str = "检查变更和相关测试。", extra: str = "") -> Path:
    directory = base / ".lancher" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 审查代码变更\n{extra}---\n{body}\n", encoding="utf-8"
    )
    return directory


def test_project_override_retains_qualified_user_id_and_body_is_not_catalog_metadata(tmp_path: Path) -> None:
    project, user = tmp_path / "project", tmp_path / "user"
    write_skill(user, body="用户级私有正文。")
    write_skill(project, body="项目级私有正文。")
    service = SkillsService(project, user)

    assert service.resolve("review").id == "project/review"
    assert service.resolve("user/review").shadowed
    assert len(service.list_skills()) == 2
    catalog = service.catalog_prompt()
    assert "project/review" in catalog
    assert "user/review" not in catalog
    assert "私有正文" not in catalog
    assert all("body" not in item.to_dict() for item in service.list_skills())
    assert service.load("user/review").body == "用户级私有正文。"


@pytest.mark.parametrize("content,code", [
    ("缺少 frontmatter", "invalid_skill_format"),
    ("---\nname: other\ndescription: 文案\n---\n正文", "skill_name_mismatch"),
    ("---\nname: Review\ndescription: 文案\n---\n正文", "invalid_skill_name"),
    ("---\nname: review\ndescription: [bad]\n---\n正文", "invalid_skill_description"),
    ("---\nname: review\ndescription: 文案\ndisable-model-invocation: nope\n---\n正文", "invalid_skill_format"),
    ("---\nname: review\ndescription: 文案\n---\n", "invalid_skill_format"),
    ("---\n!!python/object/apply:os.system ['echo unwanted']\n---\n正文", "invalid_skill_format"),
])
def test_invalid_skill_isolated_from_other_skills(tmp_path: Path, content: str, code: str) -> None:
    invalid = write_skill(tmp_path, "review")
    (invalid / "SKILL.md").write_text(content, encoding="utf-8")
    write_skill(tmp_path, "valid")
    service = SkillsService(tmp_path, tmp_path / "unused")

    assert [info.name for info in service.list_skills()] == ["valid"]
    assert [error.code for error in service.errors] == [code]


def test_reload_reflects_deleted_skills_and_edited_metadata(tmp_path: Path) -> None:
    directory = write_skill(tmp_path)
    service = SkillsService(tmp_path, tmp_path / "unused")
    before = service.load("review")
    (directory / "SKILL.md").write_text("---\nname: review\ndescription: 新描述\n---\n新正文", encoding="utf-8")
    after = service.load("review")

    assert after.description == "新描述"
    assert after.digest != before.digest
    assert service.resolve("review").description != after.description
    service.reload()
    assert service.resolve("review").description == "新描述"
    (directory / "SKILL.md").unlink()
    service.reload()
    with pytest.raises(SkillError, match="没有登记技能"):
        service.load("review")


def test_snapshot_round_trip_and_fingerprint(tmp_path: Path) -> None:
    directory = write_skill(tmp_path)
    service = SkillsService(tmp_path, tmp_path / "unused")
    snapshot = service.load("review")

    assert snapshot.digest == hashlib.sha256((directory / "SKILL.md").read_bytes()).hexdigest()
    assert SkillSnapshot.from_dict(snapshot.to_dict()) == snapshot
    corrupted = snapshot.to_dict() | {"id": "user/other"}
    with pytest.raises(SkillError):
        SkillSnapshot.from_dict(corrupted)


def test_catalog_budget_disabled_and_explicit_only_skill(tmp_path: Path) -> None:
    write_skill(tmp_path, "review")
    write_skill(tmp_path, "manual", extra="disable-model-invocation: true\n")
    service = SkillsService(tmp_path, tmp_path / "unused")

    assert "manual" not in service.catalog_prompt()
    assert service.load("manual").body
    assert service.catalog_prompt(disabled={"project/review"}) == ""
    assert len(service.catalog_prompt(max_chars=180)) <= 180
    assert service.catalog_prompt(max_chars=5) == ""
    assert service.catalog_prompt(max_chars=0) == ""


def test_explicit_mentions_are_known_deduplicated_and_not_shell_variables(tmp_path: Path) -> None:
    for name in ("review", "plan", "env"):
        write_skill(tmp_path, name)
    service = SkillsService(tmp_path, tmp_path / "unused")

    assert service.explicit_mentions("用 $review 帮忙，之后 $plan。再用 $review") == ["project/review", "project/plan"]
    assert service.explicit_mentions("指定 `$project/review` 和 $missing") == ["project/review"]
    assert service.explicit_mentions("$HOME ${review} $(review) $env:PATH \\$review $review/path $plan='test'") == []
    assert service.explicit_mentions("```powershell\necho $review\n```\necho $plan\n$plan.Name") == []


@pytest.mark.parametrize("path", ["../secret.txt", "references/../../secret.txt", "/secret.txt", "C:\\secret.txt", "C:secret.txt", "\\\\server\\secret", "secret.txt:stream"])
def test_resource_rejects_absolute_traversal_and_alternate_streams(tmp_path: Path, path: str) -> None:
    write_skill(tmp_path)
    service = SkillsService(tmp_path, tmp_path / "unused")
    with pytest.raises(SkillError) as caught:
        service.read_resource("review", path)
    assert caught.value.code == "skill_resource_outside_root"


def test_resource_requires_registered_skill_utf8_file_and_valid_paging(tmp_path: Path) -> None:
    directory = write_skill(tmp_path)
    (directory / "references").mkdir()
    (directory / "binary.txt").write_bytes(b"\x00\x01")
    service = SkillsService(tmp_path, tmp_path / "unused")
    for name, path, code in (("other", "notes.txt", "skill_not_found"), ("review", "missing", "skill_resource_not_found"),
                             ("review", "references", "skill_resource_not_file"), ("review", "binary.txt", "skill_decode_error"),
                             ("review", "SKILL.md", "skill_body_requires_loading")):
        with pytest.raises(SkillError) as caught:
            service.read_resource(name, path)
        assert caught.value.code == code
    for start, count in ((0, 1), (True, 1), (1, 401), (1, False)):
        with pytest.raises(SkillError) as caught:
            service.read_resource("review", "notes.txt", start, count)
        assert caught.value.code == "invalid_arguments"


def test_resource_paging_preserves_complete_lines_and_returns_next_line(tmp_path: Path) -> None:
    directory = write_skill(tmp_path)
    (directory / "notes.txt").write_text("\n".join(f"资料 {index}" for index in range(5)), encoding="utf-8")
    service = SkillsService(tmp_path, tmp_path / "unused")
    page = service.read_resource("review", "notes.txt", 2, 2)
    assert page.text == "资料 1\n资料 2"
    assert (page.start_line, page.end_line, page.total_lines, page.next_line) == (2, 3, 5, 4)
    assert page.truncated
    last = service.read_resource("review", "notes.txt", page.next_line, 2)
    assert last.text == "资料 3\n资料 4"
    assert last.next_line is None
    assert not last.truncated


def test_budget_rejects_oversize_body_and_files_and_limits_resource_pages(tmp_path: Path) -> None:
    oversized_body = write_skill(tmp_path, "large", body="x" * (MAX_SKILL_BODY_CHARS + 1))
    oversized_file = write_skill(tmp_path, "huge", body="x" * (MAX_SKILL_BYTES + 1))
    directory = write_skill(tmp_path, "review")
    (directory / "notes.txt").write_text("\n".join("x" * 1000 for _ in range(30)), encoding="utf-8")
    (directory / "line.txt").write_text("x" * (MAX_RESOURCE_CHARS + 1), encoding="utf-8")
    service = SkillsService(tmp_path, tmp_path / "unused")

    assert {error.code for error in service.errors} == {"skill_body_too_large", "skill_file_too_large"}
    page = service.read_resource("review", "notes.txt")
    assert len(page.text) <= MAX_RESOURCE_CHARS
    assert page.next_line == 12
    assert page.text.count("\n") == 10
    with pytest.raises(SkillError) as caught:
        service.read_resource("review", "line.txt")
    assert caught.value.code == "skill_resource_line_too_large"


def make_symlink(path: Path, target: Path, *, directory: bool = False) -> None:
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建符号链接：{exc}")


def test_resource_symlink_cannot_leave_skill_directory(tmp_path: Path) -> None:
    directory = write_skill(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("不应读取", encoding="utf-8")
    make_symlink(directory / "shortcut.txt", outside)
    service = SkillsService(tmp_path, tmp_path / "unused")
    with pytest.raises(SkillError) as caught:
        service.read_resource("review", "shortcut.txt")
    assert caught.value.code == "skill_resource_outside_root"


def test_skill_directory_symlink_cannot_leave_catalog_root(tmp_path: Path) -> None:
    project, user = tmp_path / "project", tmp_path / "user"
    outside = write_skill(user)
    root = project / ".lancher" / "skills"
    root.mkdir(parents=True)
    make_symlink(root / "review", outside, directory=True)
    service = SkillsService(project, tmp_path / "unused")
    assert service.list_skills() == ()
    assert [issue.code for issue in service.errors] == ["skill_path_outside_root"]


def test_entry_hardlink_cannot_bypass_controlled_skill_loading(tmp_path: Path) -> None:
    directory = write_skill(tmp_path)
    alias = directory / "instructions-alias.txt"
    alias.hardlink_to(directory / "SKILL.md")
    service = SkillsService(tmp_path, tmp_path / "unused")
    with pytest.raises(SkillError) as caught:
        service.read_resource("review", alias.name)
    assert caught.value.code == "skill_body_requires_loading"


@pytest.mark.parametrize("metadata,code", [
    ("extra: " + "x" * MAX_FRONTMATTER_CHARS, "skill_frontmatter_too_large"),
    ("extra: " + "[" * 2000 + "]" * 2000, "invalid_skill_format"),
    ("extra: [unterminated", "invalid_skill_format"),
])
def test_oversize_and_malformed_frontmatter_are_isolated(tmp_path: Path, metadata: str, code: str) -> None:
    write_skill(tmp_path, "review", extra=metadata + "\n")
    write_skill(tmp_path, "valid")
    service = SkillsService(tmp_path, tmp_path / "unused")
    assert [entry.name for entry in service.list_skills()] == ["valid"]
    assert [issue.code for issue in service.errors] == [code]
