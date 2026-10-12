"""测试入口拥有 Agent 资源，TUI 卸载只停止当前活动。"""
from __future__ import annotations

import pytest
import httpx

from lancher_code.agent.service import AgentService
from lancher_code.providers.discovery import ModelDiscoveryClient


@pytest.fixture(autouse=True)
def isolate_default_model_discovery(monkeypatch):
    """默认界面测试只得到空列表；发现流程测试显式注入自己的模拟传输。"""
    initialize = ModelDiscoveryClient.__init__
    def isolated(self, *args, **kwargs):
        if not args and 'client_factory' not in kwargs:
            kwargs['client_factory'] = lambda **options: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'data': []})), **options)
        initialize(self, *args, **kwargs)
    monkeypatch.setattr(ModelDiscoveryClient, '__init__', isolated)


@pytest.fixture(autouse=True)
async def close_test_agents(monkeypatch):
    agents: list[AgentService] = []
    initialize = AgentService.__init__

    def tracked_initialize(agent, *args, **kwargs):
        initialize(agent, *args, **kwargs)
        agents.append(agent)

    monkeypatch.setattr(AgentService, "__init__", tracked_initialize)
    yield
    for agent in reversed(agents):
        await agent.close()
