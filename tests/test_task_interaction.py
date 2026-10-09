from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lancher_code.errors import ConfigError, ProviderRequestError
from lancher_code.models import MessageUsage, PermissionResolution, StreamEvent, ToolCallChunk, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.builtin.write_plan_file import WritePlanFileTool
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import TurnRunner


def reply(text: str, tokens: int = 1) -> list[StreamEvent]:
    return [StreamEvent(kind="text_delta", text=text), StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=tokens, output_tokens=1))]


def calls(*items: tuple[str, dict]) -> list[StreamEvent]:
    return [StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
        call_index=index, provider_call_id=f"call-{index}", name_delta=name,
        arguments_delta=json.dumps(args))) for index, (name, args) in enumerate(items)] + [StreamEvent(kind="message_end")]


class Provider:
    def __init__(self, responses, gate: asyncio.Event | None = None):
        self.responses = responses
        self.requests = []
        self.gate = gate
        self.started = asyncio.Event()

    async def stream_chat(self, request):
        self.requests.append(request)
        current = self.responses.pop(0)
        self.started.set()
        if self.gate is not None and len(self.requests) == 1:
            await self.gate.wait()
        if isinstance(current, Exception):
            raise current
        for event in current:
            yield event


def make_runner(provider, config, directory, *tools):
    session = SessionController(config, cwd=directory)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=directory, timeout_seconds=2))
    return runner, session


async def collect(runner, text, handler=None):
    result = []
    async for event in runner.run_user_turn(text):
        result.append(event)
        if handler is not None:
            handler(event)
    return result


@pytest.mark.asyncio
async def test_steering_preserves_user_order_and_segment_usage(openai_provider_config, tmp_path):
    gate = asyncio.Event()
    provider = Provider([reply("第一段", 3), reply("按补充继续", 5)], gate)
    runner, session = make_runner(provider, openai_provider_config, tmp_path)
    task = asyncio.create_task(collect(runner, "原任务"))
    await asyncio.wait_for(provider.started.wait(), 2)
    receipt = runner.enqueue_input("补充限制", "steer")
    assert receipt.state == "pending"
    with pytest.raises(ConfigError):
        runner.set_phase("discuss")
    with pytest.raises(ConfigError):
        runner.set_permission_policy("bypass")
    gate.set()
    events = await asyncio.wait_for(task, 3)
    assert [m.content for m in session.state.messages if m.role == "user"] == ["原任务", "补充限制"]
    assert [m.usage.input_tokens for m in session.state.messages if m.role == "assistant"] == [3, 5]
    assert session.total_usage().input_tokens == 8
    assert sum(e.kind == "turn_completed" for e in events) == 1
    assert sum(e.kind == "assistant_message_completed" for e in events) == 2
    assert not runner.pending_inputs


class GatedRead:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    @property
    def definition(self):
        return ToolDefinition(name="read_file", description="测试读取", category="read")

    async def execute(self, args, context):
        self.started.set()
        await self.finish.wait()
        return ToolExecutionResult(call_id="", tool_name="read_file", content="读取完成", is_error=False)


@pytest.mark.asyncio
async def test_steering_waits_for_started_group_and_skips_later_write(openai_provider_config, tmp_path):
    read = GatedRead()
    provider = Provider([calls(("read_file", {"path": "a.txt"}), ("write_file", {"path": "new.txt", "content": "不应写入"})), reply("按补充调整")])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, read, WriteFileTool())
    runner.set_permission_policy("bypass")
    task = asyncio.create_task(collect(runner, "检查再修改"))
    await asyncio.wait_for(read.started.wait(), 2)
    runner.enqueue_input("先不要改文件", "steer")
    read.finish.set()
    events = await asyncio.wait_for(task, 3)
    assert not (tmp_path / "new.txt").exists()
    results = [e.tool_result for e in events if e.kind == "tool_result_received"]
    assert results[0].ok
    assert results[1].error_code == "steering_superseded"
    uses = [b.call_id for m in session.transcript for b in m.blocks if b.kind == "tool_use"]
    outputs = [b.call_id for m in session.transcript for b in m.blocks if b.kind == "tool_result"]
    assert uses == outputs


@pytest.mark.asyncio
async def test_steering_closes_pending_approval_without_writing_or_allow_rule(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_file", {"path": "new.txt", "content": "不应写入"})), reply("已调整")])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())
    request_ids = []

    def handle(event):
        if event.kind == "permission_request_created":
            assert event.message is not None
            assert event.message.trace.entries[-1].metadata["state"] == "awaiting_permission"
            request_ids.append(event.permission_request.request_id)
            runner.enqueue_input("不要写入", "steer")

    events = await asyncio.wait_for(collect(runner, "修改文件", handle), 3)
    assert request_ids
    assert any(e.kind == "permission_request_closed" for e in events)
    assert any(e.tool_result and e.tool_result.error_code == "steering_superseded" for e in events)
    assert not (tmp_path / "new.txt").exists()
    assert runner.resolve_permission_request(PermissionResolution(request_id=request_ids[0], outcome="allow_project")) is False
    original_assistant = next(message for message in session.state.messages if message.role == "assistant")
    result = next(entry for entry in original_assistant.trace.entries if entry.kind == "tool_result")
    assert result.metadata["state"] == "skipped"
    assert result.metadata["error_code"] == "steering_superseded"
    assert result.metadata["started"] is False


@pytest.mark.asyncio
async def test_denied_approval_is_recorded_without_claiming_tool_executed(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_file", {"path": "new.txt", "content": "不应写入"})), reply("已拒绝")])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())

    def handle(event):
        if event.kind == "permission_request_created":
            assert event.message.trace.entries[-1].metadata["started"] is False
            runner.resolve_permission_request(PermissionResolution(event.permission_request.request_id, "deny"))

    events = await asyncio.wait_for(collect(runner, "修改文件", handle), 3)
    assert not (tmp_path / "new.txt").exists()
    assert not any(event.kind == "progress_updated" and event.tool_call is not None for event in events)
    entries = session.state.messages[-1].trace.entries
    call = next(entry for entry in entries if entry.kind == "tool_call")
    result = next(entry for entry in entries if entry.kind == "tool_result")
    assert call.metadata["started"] is False
    assert result.metadata["started"] is False
    assert result.metadata["error_code"] == "permission_user_denied"


@pytest.mark.asyncio
async def test_cancel_during_approval_pauses_queue(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_file", {"path": "new.txt", "content": "不应写入"}))])
    runner, _ = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())

    def handle(event):
        if event.kind == "permission_request_created":
            runner.enqueue_input("之后检查")
            assert runner.cancel_active_turn()

    events = await asyncio.wait_for(collect(runner, "修改文件", handle), 3)
    assert any(e.kind == "turn_cancelled" for e in events)
    assert any(e.kind == "permission_request_closed" for e in events)
    assert runner.pending_inputs[0].state == "paused"
    assert not [e async for e in runner.run_next_queued_turn()]
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.asyncio
async def test_queue_fifo_edit_conversion_and_late_steering(openai_provider_config, tmp_path):
    gate = asyncio.Event()
    provider = Provider([reply("完成"), reply("补充回复"), reply("下一轮回复")], gate)
    runner, session = make_runner(provider, openai_provider_config, tmp_path)
    task = asyncio.create_task(collect(runner, "原任务"))
    await asyncio.wait_for(provider.started.wait(), 2)
    first = runner.enqueue_input("先排队")
    runner.update_pending_input(first.id, "改好的补充")
    runner.convert_pending_input(first.id, "steer")
    second = runner.enqueue_input("下一轮")
    gate.set()
    await asyncio.wait_for(task, 3)
    with pytest.raises(ConfigError):
        runner.convert_pending_input(first.id, "steer")
    assert runner.pending_inputs[0].id == second.id
    assert [e async for e in runner.run_next_queued_turn()]
    assert [m.content for m in session.state.messages if m.role == "user"] == ["原任务", "改好的补充", "下一轮"]
    late = runner.enqueue_input("迟到的补充", "steer")
    assert late.state == "paused"
    assert not [e async for e in runner.run_next_queued_turn()]


@pytest.mark.asyncio
async def test_completion_boundary_retains_late_input_without_reopening_task(openai_provider_config, tmp_path):
    provider = Provider([reply("完成")])
    runner, _ = make_runner(provider, openai_provider_config, tmp_path)

    def handle(event):
        if event.kind == "assistant_message_completed":
            runner.enqueue_input("晚到", "steer")

    events = await collect(runner, "任务", handle)
    assert len(provider.requests) == 1
    assert runner.pending_inputs[0].state == "paused"
    assert sum(e.kind == "turn_completed" for e in events) == 1


@pytest.mark.asyncio
async def test_plan_execution_uses_bound_content_and_rejects_duplicate(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_plan_file", {"content": "1. 修改保存流程\n2. 测试"})), reply("计划已准备")])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WritePlanFileTool())
    runner.set_phase("plan")
    runner.set_permission_policy("bypass")
    await collect(runner, "制定计划")
    snapshot = session.plan_snapshot
    assert snapshot is not None and snapshot.ready
    session.plan_file_path.write_text("另一个会话覆盖的内容", encoding="utf-8")
    with pytest.raises(ConfigError):
        runner.prepare_plan_execution(session.session_id, "旧版本")
    text = runner.prepare_plan_execution(session.session_id, snapshot.digest)
    assert "修改保存流程" in text and "另一个会话" not in text
    assert session.work_phase == "execute" and session.permission_policy == "bypass"
    with pytest.raises(ConfigError):
        runner.prepare_plan_execution(session.session_id, snapshot.digest)


@pytest.mark.asyncio
async def test_queue_persistence_restores_paused_without_sending(openai_provider_config, tmp_path):
    provider = Provider([])
    runner, session = make_runner(provider, openai_provider_config, tmp_path)
    runner.enqueue_input("恢复后由我决定")
    session.save_session("待办")
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session("待办")
    assert restored.session_id == session.session_id
    assert restored.pending_inputs[0].text == "恢复后由我决定"
    assert restored.pending_inputs[0].state == "paused"
    assert not provider.requests


@pytest.mark.asyncio
async def test_explicit_steering_runs_even_when_follow_up_queue_is_paused(openai_provider_config, tmp_path):
    gate = asyncio.Event()
    provider = Provider([reply("当前回复"), reply("补充回复")], gate)
    runner, session = make_runner(provider, openai_provider_config, tmp_path)
    queued = runner.enqueue_input("暂缓的下一轮")
    runner.pause_queue()
    task = asyncio.create_task(collect(runner, "新的当前任务"))
    await asyncio.wait_for(provider.started.wait(), 2)
    receipt = runner.enqueue_input("明确补充", "steer")
    assert receipt.state == "pending"
    gate.set()
    await asyncio.wait_for(task, 3)
    assert runner.pending_inputs[0].id == queued.id
    assert runner.pending_inputs[0].state == "paused"
    assert session.state.messages[-2].content == "明确补充"


@pytest.mark.asyncio
async def test_removed_steering_keeps_revoked_approval_distinct_from_denial(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_file", {"path": "new.txt", "content": "不应写入"})), reply("调整后继续")])
    runner, _ = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())

    def handle(event):
        if event.kind == "permission_request_created":
            item = runner.enqueue_input("中途补充", "steer")
            runner.remove_pending_input(item.id)

    events = await asyncio.wait_for(collect(runner, "修改文件", handle), 3)
    assert not (tmp_path / "new.txt").exists()
    assert any(e.tool_result and e.tool_result.error_code == "steering_superseded" for e in events)
    assert not any(e.kind == "permission_request_resolved" for e in events)


@pytest.mark.asyncio
async def test_closing_event_consumer_cancels_approval_and_pauses_queue(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_file", {"path": "new.txt", "content": "不应写入"}))])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())
    stream = runner.run_user_turn("修改文件")
    async for event in stream:
        if event.kind == "permission_request_created":
            runner.enqueue_input("保留的下一轮")
            break
    await asyncio.wait_for(stream.aclose(), 3)
    assert not runner.has_active_turn
    assert runner.pending_inputs[0].state == "paused"
    assert session.state.messages[-1].status == "cancelled"
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failure", "cancel"])
async def test_unsuccessful_plan_replacement_never_reactivates_old_snapshot(openai_provider_config, tmp_path, ending):
    provider = Provider([
        calls(("write_plan_file", {"content": "正在修改的新计划"})),
        ProviderRequestError("模拟连接中断"),
    ])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WritePlanFileTool())
    runner.set_phase("plan")
    runner.set_permission_policy("bypass")
    previous = session.set_plan_snapshot("此前完成的旧计划", source_message_id="previous", ready=True)
    runner.enqueue_input("下一轮应暂停")

    def handle(event):
        if ending == "cancel" and event.kind == "tool_result_received":
            runner.cancel_active_turn()

    events = await collect(runner, "重新修改计划", handle)
    assert any(event.kind in {"turn_failed", "turn_cancelled"} for event in events)
    assert session.plan_snapshot is not None and not session.plan_snapshot.ready
    assert runner.queue_paused
    for digest in (previous.digest, session.plan_snapshot.digest):
        with pytest.raises(ConfigError):
            runner.prepare_plan_execution(session.session_id, digest)


@pytest.mark.asyncio
async def test_repeated_stop_does_not_interrupt_tool_cleanup(openai_provider_config, tmp_path):
    class CleanupRead(GatedRead):
        def __init__(self):
            super().__init__()
            self.cleaning = asyncio.Event()
            self.cleaned = False

        async def execute(self, args, context):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cleaning.set()
                await self.finish.wait()
                self.cleaned = True
                raise

    tool = CleanupRead()
    runner, _ = make_runner(Provider([calls(("read_file", {"path": "a.txt"}))]), openai_provider_config, tmp_path, tool)
    task = asyncio.create_task(collect(runner, "读取文件"))
    await asyncio.wait_for(tool.started.wait(), 2)
    assert runner.cancel_active_turn()
    await asyncio.wait_for(tool.cleaning.wait(), 2)
    assert runner.cancel_active_turn()
    tool.finish.set()
    events = await asyncio.wait_for(task, 3)
    assert tool.cleaned
    assert sum(event.kind == "turn_cancelled" for event in events) == 1
    assert not runner.has_active_turn
