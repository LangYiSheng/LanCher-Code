from __future__ import annotations

import asyncio

from lancher_code.contracts.tools import ToolDefinition, ToolExecutionResult, ToolPermissionMetadata
from lancher_code.execution.contracts import ResourceClaim
from lancher_code.execution.scheduler import path_claim
from lancher_code.filesystem.access import PathWriteDeniedError
from lancher_code.filesystem.deletion import DeleteInterruptedError, DeletePlan, execute_delete, plan_delete, resolve_delete_target
from lancher_code.tools.context import ToolContext
from lancher_code.tools.core.base import build_tool_error, build_tool_success


class DeleteFileTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="delete_file",
            description=("删除指定文件、目录或链接，外部路径按访问权限审批。目录删除必须显式设置 recursive=true。"
                         "链接只删除链接本身，不跟随目标。不能整体删除项目根目录、磁盘根目录、"
                         "系统关键目录、用户主目录或会话内部记录；讨论和计划阶段只读。"),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的精确路径；不支持通配符。"},
                    "recursive": {"type": "boolean", "default": False,
                                  "description": "删除目录时必须显式为 true；不跟随目录中的链接。"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            category="write",
            allowed_phases=("execute",),
            permission=ToolPermissionMetadata(source="builtin", rule_key="delete_file", display_name="DeleteFile"),
        )

    def resource_claims(self, arguments: dict[str, object], context: ToolContext) -> tuple[ResourceClaim, ...]:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise PathWriteDeniedError("invalid_arguments", "path 必须是非空文件路径。")
        target = resolve_delete_target(raw_path, context)
        if target.is_symlink() or target.is_junction():
            # 锁定链接的真实父目录，避免规范化末级链接时锁到其指向目标。
            return (path_claim(target.parent, write=True, recursive=True),)
        # 资源声明只检查单个目标，完整目录清单由后台权限检查生成。
        return (path_claim(target, write=True, recursive=target.is_dir()),)

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        try:
            # 执行器可传入审批前冻结的清单，工具直接调用时则即时生成。
            plan = getattr(context, "delete_plan", None)
            if plan is None:
                plan = await asyncio.to_thread(plan_delete, arguments, context)
            if not isinstance(plan, DeletePlan):
                raise PathWriteDeniedError("invalid_delete_plan", "删除清单无效。")
            raw_path = arguments.get("path")
            recursive = arguments.get("recursive", False)
            if not isinstance(raw_path, str) or not raw_path.strip() or not isinstance(recursive, bool):
                raise PathWriteDeniedError("invalid_arguments", "path 必须是非空字符串，recursive 必须是布尔值。")
            if resolve_delete_target(raw_path, context) != plan.target or recursive != plan.recursive:
                raise PathWriteDeniedError("delete_target_changed", "实际调用与已确认的删除清单不匹配。")
            return await self.execute_plan(plan, context)
        except PathWriteDeniedError as exc:
            return self._error(exc)

    async def execute_plan(self, plan: DeletePlan, context: ToolContext) -> ToolExecutionResult:
        try:
            deleted = await execute_delete(plan, context)
        except PathWriteDeniedError as exc:
            return self._error(exc)
        metadata = {"path": str(plan.target), "recursive": plan.recursive,
                    "deleted_count": len(deleted), "deleted_paths": [str(path) for path in deleted],
                    "manifest_signature": plan.signature, "bytes_deleted": plan.total_bytes}
        return build_tool_success(
            summary=f"已删除 {plan.target.name}",
            content=f"已删除 {plan.target}\n删除项数：{len(deleted)}",
            metadata=metadata, tool_name=self.definition.name,
        )

    def _error(self, exc: ValueError) -> ToolExecutionResult:
        metadata: dict[str, object] = {}
        if isinstance(exc, DeleteInterruptedError):
            metadata = {"partial": True, "deleted_count": len(exc.deleted_paths),
                        "deleted_paths": [str(path) for path in exc.deleted_paths], "automatic_retry": False}
        return build_tool_error(summary="删除失败", error_code=(
            exc.reason_code if isinstance(exc, PathWriteDeniedError) else "invalid_access_target"),
            error_message=str(exc), metadata=metadata, tool_name=self.definition.name)
