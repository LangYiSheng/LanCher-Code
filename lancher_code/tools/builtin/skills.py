"""模型读取技能的入口；正文由智能体核心受控注入，不留在工具历史里。"""
from __future__ import annotations

from collections.abc import Callable

from lancher_code.agent.skills import SkillError, SkillSnapshot, SkillsService
from lancher_code.agent.skills.service import DEFAULT_RESOURCE_LINES, MAX_RESOURCE_LINES
from lancher_code.contracts.tools import ToolDefinition, ToolExecutionResult
from lancher_code.execution.contracts import ResourceClaim
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.base import build_tool_error, build_tool_success


class LoadSkillTool:
    def __init__(
        self, service: SkillsService, on_load: Callable[[SkillSnapshot], None],
        is_enabled: Callable[[str], bool] | None = None,
    ) -> None:
        self._service = service
        self._on_load = on_load
        self._is_enabled = is_enabled or (lambda _: True)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="load_skill",
            description=(
                "任务与可用技能描述匹配时加载技能。输入技能名称或 project/名称、user/名称。"
                "成功后完整指令在下一次模型请求中提供，并跨轮生效直到压缩或主动卸载。"
                "技能不会扩大执行权限；引用文件用 read_skill_resource 读取，脚本须用普通命令工具执行。"
            ),
            input_schema={
                "type": "object", "properties": {
                    "skill": {"type": "string", "description": "已登记技能的名称或带来源的稳定 ID。"},
                }, "required": ["skill"], "additionalProperties": False,
            },
            category="read", is_system_tool=True,
            allowed_phases=("discuss", "plan", "execute"),
        )

    def resource_claims(self, arguments: dict[str, object], context: ToolContext) -> tuple[ResourceClaim, ...]:
        skill = arguments.get("skill")
        if not isinstance(skill, str):
            return ()
        try:
            path = self._service.resource_path(skill, "SKILL.md")
        except (SkillError, OSError):
            return ()
        return (ResourceClaim("path", str(path.resolve()), "shared", lifetime="invocation"),)

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        try:
            skill = arguments.get("skill")
            if not isinstance(skill, str) or not skill.strip():
                raise SkillError("invalid_arguments", "skill 必须是非空字符串。")
            info = self._service.resolve(skill)
            if not self._is_enabled(info.id):
                raise SkillError("skill_disabled", f"技能 {info.id} 已被禁用。")
            snapshot = self._service.load(info.id)
            self._on_load(snapshot)
        except (SkillError, OSError) as exc:
            return _error("加载技能失败", self.definition.name, exc)
        return build_tool_success(
            summary=f"已加载技能 {snapshot.id}",
            content=f"已加载技能 {snapshot.id}，完整指令将在下一次模型请求中提供。",
            metadata={"skill_id": snapshot.id, "skill_digest": snapshot.digest, "skill_scope": snapshot.scope},
            tool_name=self.definition.name,
        )


class ReadSkillResourceTool:
    def __init__(self, service: SkillsService, is_enabled: Callable[[str], bool] | None = None) -> None:
        self._service = service
        self._is_enabled = is_enabled or (lambda _: True)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_skill_resource",
            description=(
                "读取已登记技能目录中的 UTF-8 引用资料或脚本源码，不执行脚本。"
                "必须提供技能名称或稳定 ID，以及技能目录内的相对路径。"
                "文件按行分页，start_line 从 1 开始；按 next_line 继续读取。"
            ),
            input_schema={
                "type": "object", "properties": {
                    "skill": {"type": "string", "description": "已登记技能的名称或带来源的稳定 ID。"},
                    "relative_path": {"type": "string", "description": "相对于该技能目录的文件路径。"},
                    "start_line": {"type": "integer", "minimum": 1, "description": "可选，起始行号，从 1 开始。"},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": MAX_RESOURCE_LINES,
                                   "description": f"可选，返回行数，默认 {DEFAULT_RESOURCE_LINES}，上限 {MAX_RESOURCE_LINES}。"},
                }, "required": ["skill", "relative_path"], "additionalProperties": False,
            },
            category="read", is_system_tool=True,
            allowed_phases=("discuss", "plan", "execute"),
        )

    def resource_claims(self, arguments: dict[str, object], context: ToolContext) -> tuple[ResourceClaim, ...]:
        skill, relative_path = arguments.get("skill"), arguments.get("relative_path")
        if not isinstance(skill, str) or not isinstance(relative_path, str):
            return ()
        try:
            path = self._service.resource_path(skill, relative_path)
        except (SkillError, OSError):
            return ()
        return (ResourceClaim("path", str(path.resolve()), "shared", lifetime="invocation"),)

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        try:
            skill, relative_path = arguments.get("skill"), arguments.get("relative_path")
            if not isinstance(skill, str) or not skill.strip():
                raise SkillError("invalid_arguments", "skill 必须是非空字符串。")
            if not isinstance(relative_path, str) or not relative_path.strip():
                raise SkillError("invalid_arguments", "relative_path 必须是非空字符串。")
            info = self._service.resolve(skill)
            if not self._is_enabled(info.id):
                raise SkillError("skill_disabled", f"技能 {info.id} 已被禁用。")
            resource = self._service.read_resource(
                info.id, relative_path, arguments.get("start_line", 1),
                arguments.get("line_count", DEFAULT_RESOURCE_LINES),
            )
        except (SkillError, OSError) as exc:
            return _error("读取技能资源失败", self.definition.name, exc)
        selected_lines = resource.text.split("\n") if resource.end_line >= resource.start_line else ()
        numbered = "\n".join(
            f"{resource.start_line + index}\t{line}" for index, line in enumerate(selected_lines)
        )
        content = (
            f"技能资源: {resource.skill_id}/{resource.relative_path}\n"
            f"总行数: {resource.total_lines}\n"
            f"返回范围: {resource.start_line}-{resource.end_line}\n"
            f"{numbered or '(该范围内没有内容)'}"
        )
        if resource.next_line is not None:
            content += f"\n下一页 start_line={resource.next_line}。"
        metadata = resource.to_dict()
        metadata.pop("text")
        return build_tool_success(
            summary=f"已读取技能资源 {resource.relative_path}", content=content,
            metadata=metadata, tool_name=self.definition.name,
        )


def _error(summary: str, tool_name: str, exc: SkillError | OSError) -> ToolExecutionResult:
    return build_tool_error(
        summary=summary, error_code=getattr(exc, "code", "skill_read_error"),
        error_message=str(exc), tool_name=tool_name,
    )
