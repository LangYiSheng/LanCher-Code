"""测试入口拥有 Agent 资源，TUI 卸载只停止当前活动。"""
from __future__ import annotations

import pytest

from lancher_code.agent.service import AgentService


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
