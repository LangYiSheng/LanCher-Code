"""技能领域数据：目录元数据与激活正文分开保存。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


SkillScope = Literal["project", "user"]


class SkillError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SkillInfo:
    id: str
    name: str
    description: str
    scope: SkillScope
    path: str
    directory: str
    shadowed: bool = False
    auto_load: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    id: str
    name: str
    description: str
    scope: SkillScope
    path: str
    directory: str
    digest: str
    body: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SkillSnapshot:
        fields = ("id", "name", "description", "scope", "path", "directory", "digest", "body")
        if any(not isinstance(data.get(key), str) for key in fields):
            raise SkillError("invalid_snapshot", "技能快照缺少有效的字符串字段。")
        if data["scope"] not in ("project", "user") or data["id"] != f"{data['scope']}/{data['name']}":
            raise SkillError("invalid_snapshot", "技能快照的来源与 ID 不一致。")
        if len(str(data["body"])) > 24_000:
            raise SkillError("skill_body_too_large", "技能快照正文超过上下文预算。")
        return cls(**{key: data[key] for key in fields})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SkillResource:
    skill_id: str
    path: str
    relative_path: str
    text: str
    total_lines: int
    start_line: int
    end_line: int
    next_line: int | None
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
