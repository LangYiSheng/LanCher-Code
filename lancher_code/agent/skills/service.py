"""只扫描约定目录，模型只能通过已登记技能读取引用资源。"""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

import yaml

from lancher_code.agent.skills.models import SkillDiagnostic, SkillError, SkillInfo, SkillResource, SkillSnapshot

MAX_SKILL_BYTES = 64 * 1024
MAX_SKILL_BODY_CHARS = 24_000
MAX_FRONTMATTER_CHARS = 8_000
MAX_DESCRIPTION_CHARS = 1024
MAX_RESOURCE_BYTES = 1024 * 1024
MAX_RESOURCE_CHARS = 12_000
MAX_RESOURCE_LINES = 400
DEFAULT_RESOURCE_LINES = 200
_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MENTION_PATTERN = re.compile(
    r"(?<![\w\\$])\$(?:(project|user)/)?([a-z0-9]+(?:-[a-z0-9]+)*)(?![\w/\\:\-])"
)


class SkillsService:
    def __init__(self, project_root: Path, user_root: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.user_root = Path(user_root).resolve() if user_root is not None else Path.home().resolve()
        self._skills: dict[str, SkillInfo] = {}
        self._preferred: dict[str, str] = {}
        self.errors: tuple[SkillDiagnostic, ...] = ()
        self.reload()

    def reload(self) -> tuple[SkillInfo, ...]:
        skills: dict[str, SkillInfo] = {}
        diagnostics: list[SkillDiagnostic] = []
        for scope, base in (("user", self.user_root), ("project", self.project_root)):
            root = base / ".lancher" / "skills"
            try:
                if not root.resolve().is_relative_to(base):
                    raise SkillError("skill_path_outside_root", "技能目录链接指向来源目录之外。")
                if not root.exists():
                    continue
                if not root.is_dir():
                    raise SkillError("invalid_skill_directory", "技能目录不是文件夹。")
                children = sorted(root.iterdir(), key=lambda item: item.name)
            except (SkillError, OSError) as exc:
                diagnostics.append(self._diagnostic(root, exc))
                continue
            for child in children:
                try:
                    if not child.is_dir():
                        continue
                    directory = child.resolve()
                    if not directory.is_relative_to(root.resolve()):
                        raise SkillError("skill_path_outside_root", "技能文件夹链接越过技能目录。")
                    path = child / "SKILL.md"
                    if not path.exists():
                        continue
                    text, _ = self._read_checked(path, directory, MAX_SKILL_BYTES)
                    name, description, _, auto_load = self._parse(text, child.name)
                    skill_id = f"{scope}/{name}"
                    skills[skill_id] = SkillInfo(
                        id=skill_id, name=name, description=description, scope=scope,
                        path=str(path.absolute()), directory=str(directory), auto_load=auto_load,
                    )
                except (SkillError, OSError) as exc:
                    diagnostics.append(self._diagnostic(child / "SKILL.md", exc))
        preferred: dict[str, str] = {}
        for info in skills.values():
            if info.name not in preferred or info.scope == "project":
                preferred[info.name] = info.id
        self._skills = {
            skill_id: replace(info, shadowed=preferred[info.name] != skill_id)
            for skill_id, info in skills.items()
        }
        self._preferred = preferred
        self.errors = tuple(diagnostics)
        return self.list_skills()

    def list_skills(self) -> tuple[SkillInfo, ...]:
        return tuple(sorted(self._skills.values(), key=lambda info: (info.name, info.scope)))

    def resolve(self, name_or_id: str) -> SkillInfo:
        if not isinstance(name_or_id, str) or not name_or_id.strip():
            raise SkillError("invalid_arguments", "技能名称或 ID 必须是非空字符串。")
        requested = name_or_id.strip()
        skill_id = requested if requested in self._skills else self._preferred.get(requested)
        if skill_id is None:
            raise SkillError("skill_not_found", f"没有登记技能：{requested}。请刷新技能目录后重试。")
        return self._skills[skill_id]

    def load(self, name_or_id: str) -> SkillSnapshot:
        info = self.resolve(name_or_id)
        self._ensure_registered_directory(info)
        text, data = self._read_checked(Path(info.path), Path(info.directory), MAX_SKILL_BYTES)
        name, description, body, _ = self._parse(text, info.name)
        return SkillSnapshot(
            id=info.id, name=name, description=description, scope=info.scope,
            path=info.path, directory=info.directory, digest=hashlib.sha256(data).hexdigest(), body=body,
        )

    def resource_path(self, name_or_id: str, relative_path: str) -> Path:
        """资源调度与实际读取都使用同一条路径校验。"""
        info = self.resolve(name_or_id)
        self._ensure_registered_directory(info)
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise SkillError("invalid_arguments", "relative_path 必须是非空相对路径。")
        normalized = relative_path.replace("\\", "/")
        posix = PurePosixPath(normalized)
        windows = PureWindowsPath(relative_path)
        if (posix.is_absolute() or windows.drive or windows.root
                or ".." in posix.parts or ":" in normalized or "\x00" in normalized):
            raise SkillError("skill_resource_outside_root", "技能资源必须是技能目录内的相对路径，不能包含上级目录。")
        root = Path(info.directory)
        path = root.joinpath(*posix.parts)
        if not path.resolve().is_relative_to(root):
            raise SkillError("skill_resource_outside_root", "技能资源链接指向技能目录之外。")
        return path

    def read_resource(
        self, name_or_id: str, relative_path: str, start_line: int = 1,
        line_count: int = DEFAULT_RESOURCE_LINES,
    ) -> SkillResource:
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise SkillError("invalid_arguments", "start_line 必须是从 1 开始的整数。")
        if isinstance(line_count, bool) or not isinstance(line_count, int) or not 1 <= line_count <= MAX_RESOURCE_LINES:
            raise SkillError("invalid_arguments", f"line_count 必须是 1 到 {MAX_RESOURCE_LINES} 之间的整数。")
        info = self.resolve(name_or_id)
        path = self.resource_path(info.id, relative_path)
        entry_path = Path(info.path)
        if (path.resolve() == entry_path.resolve()
                or path.exists() and entry_path.exists() and path.samefile(entry_path)):
            raise SkillError("skill_body_requires_loading", "SKILL.md 正文应通过 load_skill 加载，引用资源入口不重复返回正文。")
        text, _ = self._read_checked(path, Path(info.directory), MAX_RESOURCE_BYTES)
        lines = text.splitlines()
        offset = start_line - 1
        selected: list[str] = []
        chars = 0
        for index in range(offset, min(offset + line_count, len(lines))):
            line = lines[index]
            # 不切断一行，调用方可以继续按行翻页，避免悄悄遗漏长行的后半段。
            if len(line) + 1 > MAX_RESOURCE_CHARS:
                if not selected:
                    raise SkillError("skill_resource_line_too_large", "这一行超过资源读取字符预算，请拆分资源文件的长行。")
                break
            if chars + len(line) + 1 > MAX_RESOURCE_CHARS:
                break
            selected.append(line)
            chars += len(line) + 1
        end_line = offset + len(selected)
        more = end_line < len(lines)
        return SkillResource(
            skill_id=info.id, path=str(path), relative_path=relative_path,
            text="\n".join(selected), total_lines=len(lines), start_line=start_line,
            end_line=end_line, next_line=end_line + 1 if more else None, truncated=more,
        )

    def catalog_prompt(self, max_chars: int = 16_000, disabled: set[str] | None = None) -> str:
        if max_chars <= 0:
            return ""
        disabled = disabled or set()
        available = [info for info in self.list_skills()
                     if not info.shadowed and info.auto_load and info.id not in disabled and info.name not in disabled]
        if not available:
            return ""
        heading = (
            "可用 Skills（以下为目录元数据，正文尚未加载）：\n"
            "任务与描述匹配时调用 load_skill；加载后遵循正文，引用资料用 read_skill_resource 读取。\n"
            "技能不增加执行权限，脚本仍通过普通命令工具执行。\n"
        )
        footer = "\n（技能目录达到预算，部分技能未列出；用户仍可显式指定。）"
        result = heading
        for index, info in enumerate(available):
            description = " ".join(info.description.split())
            line = f"- {info.id} [{info.scope}]: {description}\n"
            reserve = len(footer) if index < len(available) - 1 else 0
            if len(result) + len(line) + reserve > max_chars:
                if len(result) + len(footer) <= max_chars:
                    result += footer
                return result if len(result) <= max_chars else ""
            result += line
        return result

    def explicit_mentions(self, text: str) -> list[str]:
        """只识别已登记技能；忽略转义、代码块、变量赋值和常见 shell 变量语法。"""
        without_fences = re.sub(r"```[^\n]*\n[\s\S]*?(?:```|\Z)", "", text)
        result: list[str] = []
        for match in _MENTION_PATTERN.finditer(without_fences):
            tail = without_fences[match.end():]
            line_start = without_fences.rfind("\n", 0, match.start()) + 1
            prefix = without_fences[line_start:match.start()]
            if re.match(r"\s*=", tail) or re.match(r"\.\w", tail):
                continue
            if re.match(r"\s*(?:echo|printf|export|set|setx|Write-Output|Write-Host)\b", prefix, re.IGNORECASE):
                continue
            scope, name = match.groups()
            try:
                info = self.resolve(f"{scope}/{name}" if scope else name)
            except SkillError:
                continue
            if info.id not in result:
                result.append(info.id)
        return result

    def _ensure_registered_directory(self, info: SkillInfo) -> None:
        base = self.project_root if info.scope == "project" else self.user_root
        scope_root = (base / ".lancher" / "skills").resolve()
        root = Path(info.directory)
        if (not scope_root.is_relative_to(base) or not root.resolve().is_relative_to(scope_root)
                or root.resolve() != root):
            raise SkillError("skill_path_outside_root", "登记后的技能目录发生链接变更，请刷新技能目录。")

    @staticmethod
    def _read_checked(path: Path, root: Path, max_bytes: int) -> tuple[str, bytes]:
        if not path.resolve().is_relative_to(root):
            raise SkillError("skill_resource_outside_root", "技能文件或资源链接指向技能目录之外。")
        if not path.exists():
            raise SkillError("skill_resource_not_found", f"技能文件或资源不存在：{path.name}。")
        if not path.is_file():
            raise SkillError("skill_resource_not_file", "技能资源不是普通文件。")
        # 限量读取，不信任 stat 大小，文件在检查期间增长也不会耗尽内存。
        try:
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            raise SkillError("skill_read_error", f"读取技能失败：{exc}") from exc
        if len(data) > max_bytes:
            raise SkillError("skill_file_too_large", f"技能文件超过 {max_bytes} 字节的读取预算。")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SkillError("skill_decode_error", "技能文件或资源必须是 UTF-8 文本。") from exc
        if "\x00" in text:
            raise SkillError("skill_decode_error", "技能资源包含二进制内容，不能作为文本读取。")
        return text, data

    @staticmethod
    def _parse(text: str, directory_name: str) -> tuple[str, str, str, bool]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise SkillError("invalid_skill_format", "SKILL.md 必须以 YAML frontmatter 开头。")
        end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if end is None:
            raise SkillError("invalid_skill_format", "SKILL.md 缺少 frontmatter 结束分隔符。")
        frontmatter = "".join(lines[1:end])
        if len(frontmatter) > MAX_FRONTMATTER_CHARS:
            raise SkillError("skill_frontmatter_too_large", f"技能 frontmatter 超过 {MAX_FRONTMATTER_CHARS} 字符的元数据预算。")
        try:
            metadata = yaml.safe_load(frontmatter)
        except (yaml.YAMLError, RecursionError) as exc:
            raise SkillError("invalid_skill_format", "SKILL.md frontmatter 不是有效 YAML。") from exc
        if not isinstance(metadata, dict):
            raise SkillError("invalid_skill_format", "SKILL.md frontmatter 必须是键值映射。")
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or len(name) > 64 or not _NAME_PATTERN.fullmatch(name):
            raise SkillError("invalid_skill_name", "技能名称须为不超过 64 字符的小写字母、数字和单个连字符。")
        if name != directory_name:
            raise SkillError("skill_name_mismatch", "技能名称必须与所在文件夹名称一致。")
        if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION_CHARS:
            raise SkillError("invalid_skill_description", f"技能描述必须是 1 到 {MAX_DESCRIPTION_CHARS} 字符的字符串。")
        disabled = metadata.get("disable-model-invocation", False)
        if not isinstance(disabled, bool):
            raise SkillError("invalid_skill_format", "disable-model-invocation 必须是布尔值。")
        body = "".join(lines[end + 1:]).strip()
        if not body:
            raise SkillError("invalid_skill_format", "SKILL.md 正文不能为空。")
        if len(body) > MAX_SKILL_BODY_CHARS:
            raise SkillError("skill_body_too_large", f"技能正文超过 {MAX_SKILL_BODY_CHARS} 字符的上下文预算，请拆分为引用资源。")
        return name, description.strip(), body, not disabled

    @staticmethod
    def _diagnostic(path: Path, exc: SkillError | OSError) -> SkillDiagnostic:
        return SkillDiagnostic(path=str(path), code=getattr(exc, "code", "skill_read_error"), message=str(exc))
