"""验证同一核心的嵌入运行、状态隔离、队列与资源生命周期。"""
from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import pytest
from conftest import app_config_for
from provider_helpers import complete_test_response

from lancher_code.agent.skills import SkillsService
from lancher_code.contracts.messages import StreamEvent
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.errors import ConfigError, ProviderRequestError
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.sdk import PermissionResolution, create_agent
from lancher_code.sessions.repository import ProjectSessionRepository
from lancher_code.tools import create_default_tool_registry
from lancher_code.usage.models import MessageUsage


def reply(text):
    return [StreamEvent(kind="text_delta", text=text),
            StreamEvent(kind="message_end", response_complete=True, usage=MessageUsage(input_tokens=3, output_tokens=2))]


class OfflineProvider:
    def __init__(self, responses, gate=None):
        self.responses, self.gate = list(responses), gate
        self.gate_request = 1
        self.started = asyncio.Event()
        self.requests = []

    @complete_test_response
    async def stream_chat(self, request):
        self.requests.append(request)
        current = self.responses.pop(0)
        self.started.set()
        if self.gate is not None and len(self.requests) == self.gate_request:
            await self.gate.wait()
        if isinstance(current, Exception):
            raise current
        for event in current:
            yield event


def make_agent(tmp_path, config, provider, **kwargs):
    return create_agent(app_config_for(config).to_agent_config(), cwd=tmp_path,
        provider_factory=lambda _: provider, tool_registry=create_default_tool_registry(),
        permission_storage=PermissionStorage(),
        skills_service=SkillsService(tmp_path, user_root=tmp_path / "isolated-home"),
        enable_mcp=False, **kwargs)


async def test_sdk_run_result_retains_usage_and_releases_session_lock(tmp_path, openai_provider_config):
    async with make_agent(tmp_path, openai_provider_config, OfflineProvider([reply("完成")])) as agent:
        result = await agent.run("介绍项目")
        assert result.status == "completed" and result.text == "完成"
        assert result.usage.input_tokens == 3 and result.usage.output_tokens == 2
        assert json.loads(json.dumps(result.to_dict()))["usage"]["input_tokens"] == 3
        identity = result.session_id
    with ProjectSessionRepository(tmp_path).open(identity):
        pass
    await agent.close()
    with pytest.raises(ConfigError, match="关闭"):
        await agent.run("再次运行")


async def test_sdk_events_preserve_emission_snapshot_and_queries_do_not_mutate_state(tmp_path, openai_provider_config):
    async with make_agent(tmp_path, openai_provider_config, OfflineProvider([reply("完整正文")])) as agent:
        events = [event async for event in agent.run_user_turn("开始")]
        started = next(event for event in events if event.kind == "assistant_message_started")
        completed = next(event for event in events if event.kind == "turn_completed")
        assert started.message.content == "" and completed.message.content == "完整正文"
        started.message.content = "客户端改写"
        state = agent.state
        state.messages[-1].content = "客户端改写"
        agent.get_message(completed.message.id).content = "客户端改写"
        assert agent.messages[-1].content == "完整正文"
        assert len({event.sequence for event in events}) == len(events)
        assert [event.sequence for event in events] == sorted(event.sequence for event in events)
        assert all(event.session_id == agent.session_id and event.run_id == agent.run_id for event in events)
        json.dumps([event.to_dict() for event in events])


@pytest.mark.parametrize("failed", [False, True])
async def test_sdk_owns_follow_up_continuation_and_pauses_on_failure(tmp_path, openai_provider_config, failed):
    gate = asyncio.Event()
    provider = OfflineProvider([reply("第一轮"), ProviderRequestError("离线失败") if failed else reply("第二轮")], gate)
    async with make_agent(tmp_path, openai_provider_config, provider) as agent:
        task = asyncio.create_task(agent.run("第一条"))
        await asyncio.wait_for(provider.started.wait(), 10)
        agent.enqueue_input("第二条")
        agent.enqueue_input("第三条")
        if not failed:
            provider.responses.append(reply("第三轮"))
        gate.set()
        result = await asyncio.wait_for(task, 15)
        assert [item.content for item in agent.messages if item.role == "user"] == (
            ["第一条", "第二条"] if failed else ["第一条", "第二条", "第三条"])
        assert result.status == ("failed" if failed else "completed")
        assert agent.queue_paused is failed
        assert len(agent.pending_inputs) == (1 if failed else 0)


@pytest.mark.parametrize("approve", [False, True])
async def test_sdk_headless_permission_default_denial_and_explicit_handler(tmp_path, openai_provider_config, approve):
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    arguments = json.dumps({"path": str(outside), "content": "批准后写入"})
    response = [StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
        call_index=0, provider_call_id="write-1", name_delta="write_file", arguments_delta=arguments)),
        StreamEvent(kind="message_end", response_complete=True)]
    provider = OfflineProvider([response, reply("结束")])
    async with make_agent(tmp_path, openai_provider_config, provider) as agent:
        handler = (lambda request: PermissionResolution(request.request_id, "allow_once")) if approve else None
        result = await asyncio.wait_for(agent.run("写入外部文件", permission_handler=handler), 15)
        assert result.status == "completed"
        assert outside.exists() is approve
        results = [entry for message in agent.messages for entry in message.trace.entries if entry.kind == "tool_result"]
        assert results[-1].ok is approve


async def test_sdk_close_cancels_other_consumer_and_never_starts_queued_turn(tmp_path, openai_provider_config):
    provider = OfflineProvider([reply("不应完成"), reply("不应开始")], asyncio.Event())
    agent = make_agent(tmp_path, openai_provider_config, provider)
    task = asyncio.create_task(agent.run("挂起"))
    await asyncio.wait_for(provider.started.wait(), 10)
    agent.enqueue_input("等待下一轮")
    with pytest.raises(ConfigError):
        agent.new_session()
    await asyncio.wait_for(agent.close(), 15)
    assert task.done() and not agent.has_active_turn
    assert len(provider.requests) == 1 and agent.queue_paused
    assert agent.last_result.status == "cancelled"
    await asyncio.gather(task, return_exceptions=True)


async def test_sdk_close_inside_event_consumer_does_not_deadlock(tmp_path, openai_provider_config):
    provider = OfflineProvider([reply("不应完成")], asyncio.Event())
    agent = make_agent(tmp_path, openai_provider_config, provider)
    async with aclosing(agent.run_user_turn("任务")) as events:
        async for event in events:
            if event.kind == "assistant_message_started":
                await asyncio.wait_for(agent.close(), 15)
                break
    assert not agent.has_active_turn


async def test_sdk_subscriber_isolation_idle_queue_and_session_epoch(tmp_path, openai_provider_config):
    async with make_agent(tmp_path, openai_provider_config, OfflineProvider([reply("完成")])) as agent:
        seen = []
        def observer(event):
            seen.append(event.to_dict())
            if event.message:
                event.message.content = "不能污染核心"
        remove = agent.subscribe(observer)
        agent.enqueue_input("排队")
        assert seen[-1]["kind"] == "pending_input_changed"
        agent.remove_pending_input(agent.pending_inputs[0].id)
        await agent.run("运行")
        assert agent.messages[-1].content == "完成"
        previous = agent.session_epoch
        agent.new_session()
        assert agent.session_epoch == previous + 1 and not agent.messages
        assert seen[-1]["kind"] == "session_changed"
        remove()


async def test_sdk_async_context_cancelled_during_mcp_readiness_closes_resources(tmp_path, openai_provider_config):
    class DelayedMCP:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False
        def add_progress_callback(self, callback):
            pass
        async def initialize(self, registry):
            self.started.set()
            await asyncio.Event().wait()
        async def close(self):
            self.closed = True
    manager = DelayedMCP()
    provider = OfflineProvider([reply("不应开始")])
    agent = make_agent(tmp_path, openai_provider_config, provider, mcp_manager=manager)
    async def enter():
        async with agent:
            await agent.run("任务")
    task = asyncio.create_task(enter())
    await asyncio.wait_for(manager.started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert manager.closed and not provider.requests


@pytest.mark.parametrize("use_wait_for", [False, True])
async def test_sdk_close_from_compaction_callback_reports_reentrancy_instead_of_deadlock(tmp_path, openai_provider_config, use_wait_for):
    async with make_agent(tmp_path, openai_provider_config, OfflineProvider([])) as agent:
        async def close_inside_callback(event):
            if use_wait_for:
                await asyncio.wait_for(agent.close(), 5)
            else:
                await agent.close()
        with pytest.raises(ConfigError, match="压缩回调"):
            async with asyncio.timeout(10):
                await agent.compact_context(on_activity=close_inside_callback)
        assert not agent.is_compacting
        assert all(item.status == "failed" for item in agent.compaction_activities.values())


async def test_sdk_repeated_compaction_stop_preserves_cancelled_activity(tmp_path, openai_provider_config):
    provider = OfflineProvider([reply("已有对话"), reply("不应完成的摘要")])
    async with make_agent(tmp_path, openai_provider_config, provider) as agent:
        await agent.run("准备对话")
        provider.gate = asyncio.Event()
        provider.gate_request = 2
        async def stop(event):
            if event.compaction.status == "running":
                assert agent.cancel_active_turn()
                assert agent.cancel_active_turn()
        task = asyncio.create_task(agent.compact_context(on_activity=stop))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert not agent.is_compacting
        assert [item.status for item in agent.compaction_activities.values()] == ["cancelled"]
