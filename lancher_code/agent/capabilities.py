"""智能体能力装配与运行时门面，可用于 TUI、CLI 和其他前端。"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

from lancher_code.agent.instructions import project_instructions
from lancher_code.agent.skill_context import SkillRuntime
from lancher_code.agent.skills import SkillsService
from lancher_code.logging_system import get_logger, register_sensitive_values
from lancher_code.mcp import MCPClientManager, load_mcp_config
from lancher_code.mcp.manager import MCPInitializationProgress
from lancher_code.tools.builtin.skills import LoadSkillTool, ReadSkillResourceTool


logger = get_logger('agent.capabilities')


class AgentCapabilities:
    def __init__(self, session, registry, *, ensure_idle, project_root, skills_service=None) -> None:
        self._session, self._registry = session, registry
        self._ensure_idle = ensure_idle
        self.project_root = project_root.resolve()
        self.skills = SkillRuntime(skills_service or SkillsService(self.project_root), session)
        self._manager = None
        self._startup_task = None
        self._started = False
        self._callbacks = []
        self.updating = False
        self._closed = False
        self.mcp_progress = MCPInitializationProgress(0, 0, 0, 0, 0, None, 'complete')
        session.bind_agent_context(self.context_blocks)
        for tool in (LoadSkillTool(self.skills.service, self.skills.activate, self.skills.enabled),
                     ReadSkillResourceTool(self.skills.service, self.skills.enabled)):
            registry.register(tool)

    def configure(self, mcp_manager=None, registry=None) -> None:
        if registry is not None and registry is not self._registry:
            raise ValueError('能力必须使用同一个核心工具注册表。')
        if mcp_manager is None or mcp_manager is self._manager:
            return
        if self._started:
            raise ValueError('MCP 已启动，请使用核心重载接口。')
        self._manager = mcp_manager
        mcp_manager.add_progress_callback(self._on_progress)

    def context_blocks(self, state) -> list[str]:
        instructions = project_instructions(self.project_root)
        return ([instructions] if instructions else []) + self.skills.context_blocks(state)

    def visible_tools(self, discovered_names=None):
        published = self._session.context_state.prefix_state.get('observed_tools', {})
        discovered = set(published) | set(discovered_names or ())
        current = self._registry.list_definitions(discovered_names=discovered)
        by_name = {tool.name: tool for tool in current}
        # 热刷新注册表不会重新排序已发送定义；新能力只追加在既有定义之后。
        return [by_name[name] for name in published if name in by_name] + [
            tool for tool in current if tool.name not in published
        ]

    def context_usage(self) -> dict:
        request = self._session.preview_request(
            self.visible_tools(),
            allow_tool_calls=True,
            deferred_tool_groups=self._registry.list_deferred_index(),
        )
        estimate = self._session.context_estimate(request)
        return {'tokens': estimate.tokens, 'source': estimate.source}

    def add_mcp_progress_callback(self, callback) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_mcp_progress_callback(self, callback) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _on_progress(self, progress) -> None:
        self.mcp_progress = progress
        for callback in tuple(self._callbacks):
            try:
                callback(progress)
            except Exception:
                logger.exception('event=capabilities_observer_failed')

    def start(self) -> None:
        if self._closed or self._started or self._manager is None:
            return
        self._started = True
        self._startup_task = asyncio.create_task(self._initialize(), name='agent-mcp-initialization')

    async def _initialize(self) -> None:
        try:
            await self._manager.initialize(self._registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('event=agent_mcp_initialization_failed')

    async def _stop_initialization(self) -> None:
        task, self._startup_task = self._startup_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _wait_initialization(self) -> None:
        self.start()
        if self._startup_task is not None:
            # 用户取消刷新不能顺带关闭仍在初始化的连接管理器。
            await asyncio.shield(self._startup_task)

    async def shutdown(self) -> None:
        self._closed = True
        await self._stop_initialization()
        if self._manager is not None:
            await self._manager.close()
        self._callbacks.clear()

    def list_skills(self) -> list[dict]:
        return self.skills.list_skills()

    def show_skill(self, name: str) -> str:
        return self.skills.show_skill(name)

    def reload_skills(self) -> str:
        self._ensure_idle()
        self.skills.service.reload()
        issues = len(self.skills.service.errors)
        details = '\n'.join(f'{item.path}：{item.message}' for item in self.skills.service.errors[:8])
        return f'已刷新技能目录：{len(self.list_skills())} 个技能，{issues} 个问题。' + ('\n' + details if details else '')

    def set_skill_enabled(self, name: str, enabled: bool) -> str:
        self._ensure_idle()
        return self.skills.set_enabled(name, enabled)

    def unload_skill(self, name: str) -> str:
        self._ensure_idle()
        return self.skills.unload(name)

    def mcp_status(self) -> list[dict]:
        if self._manager is None:
            return []
        return [asdict(item) for item in self._manager.status()]

    async def refresh_mcp(self, server_name=None) -> str:
        self._ensure_idle()
        if self._manager is None:
            return '尚未配置 MCP 服务器。'
        self.updating = True
        try:
            await self._wait_initialization()
            statuses = await self._manager.refresh(self._registry, server_name)
            selected = [item for item in statuses if server_name is None or item.name == server_name]
            unavailable = [item for item in selected if item.state != 'ready']
            if unavailable:
                return 'MCP 刷新后仍有不可用服务器：' + '；'.join(
                    f'{item.name}（{item.last_error or item.state}）' for item in unavailable)
            return 'MCP 工具目录已刷新。'
        finally:
            self.updating = False

    async def reconnect_mcp(self, server_name: str) -> str:
        self._ensure_idle()
        if self._manager is None:
            raise ValueError('尚未配置 MCP 服务器。')
        self.updating = True
        try:
            await self._wait_initialization()
            statuses = await self._manager.reconnect(self._registry, server_name)
            status = next(item for item in statuses if item.name == server_name)
            if status.state != 'ready':
                return f'MCP {server_name} 重连失败：{status.last_error or status.state}。'
            return f'MCP {server_name} 已重连。'
        finally:
            self.updating = False

    async def reload_mcp(self) -> str:
        self._ensure_idle()
        self.updating = True
        try:
            configs, issues = load_mcp_config(self.project_root)
            register_sensitive_values(value for config in configs for value in (*config.env.values(), *config.headers.values()))
            await self._stop_initialization()
            if self._manager is not None:
                await self._manager.shutdown(self._registry)
            if self._closed:
                raise RuntimeError('能力服务已关闭，无法应用 MCP 配置。')
            self._manager = MCPClientManager(configs, issues=issues)
            self._manager.add_progress_callback(self._on_progress)
            self._started = True
            self._startup_task = asyncio.create_task(self._initialize(), name='agent-mcp-initialization')
            # 配置应用任务属于核心；关闭设置界面只停止等待，不关闭新连接。
            await asyncio.shield(self._startup_task)
            failed = sum(item.state == 'failed' for item in self._manager.status())
            return f'MCP 配置已应用：{len(configs)} 个服务器，{failed} 个连接失败，{len(issues)} 个配置问题。'
        finally:
            self.updating = False
