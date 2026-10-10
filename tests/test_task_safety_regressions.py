from __future__ import annotations

import asyncio

import pytest

from lancher_code.models import PendingInput, PermissionResolution, ToolCall, ToolExecutionResult
from lancher_code.session import SessionController
from lancher_code.tools.builtin.write_file import WriteFileTool
from lancher_code.tools.builtin.write_plan_file import WritePlanFileTool
from test_task_interaction import Provider, calls, collect, make_runner, reply


@pytest.mark.asyncio
async def test_confirmed_plan_reaches_execution_request_with_frozen_content(openai_provider_config, tmp_path):
    provider = Provider([
        calls(("write_plan_file", {"content": "1. 修改保存流程\n2. 运行验证"})),
        reply("计划已准备"),
        reply("开始执行已确认的计划"),
    ])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WritePlanFileTool())
    runner.set_phase("plan")
    runner.set_permission_policy("bypass")
    await collect(runner, "制定计划")
    snapshot = session.plan_snapshot
    assert snapshot is not None and snapshot.ready
    session.plan_file_path.write_text("其他会话写入的旧文件", encoding="utf-8")

    events = await collect(runner, runner.prepare_plan_execution(session.session_id, snapshot.digest))

    assert len(provider.requests) == 3
    request = provider.requests[-1]
    assert request.work_phase == "execute"
    assert request.permission_policy == "bypass"
    sent_text = "\n".join(block.text for message in request.messages for block in message.blocks)
    assert snapshot.content in sent_text
    assert snapshot.digest in sent_text
    assert "其他会话写入的旧文件" not in sent_text
    assert any(event.kind == "turn_completed" for event in events)
    assert session.plan_snapshot is not None and not session.plan_snapshot.ready


@pytest.mark.asyncio
async def test_steering_after_approval_before_execution_does_not_persist_grant(openai_provider_config, tmp_path):
    provider = Provider([
        calls(("write_file", {"path": "new.txt", "content": "不应写入"})),
        reply("已按补充调整"),
    ])
    runner, _ = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())

    def handle(event):
        if event.kind == "permission_request_created":
            assert runner.resolve_permission_request(PermissionResolution(
                request_id=event.permission_request.request_id, outcome="allow_session",
            ))
            runner.enqueue_input("不要写入", "steer")

    events = await asyncio.wait_for(collect(runner, "修改文件", handle), 3)
    assert not (tmp_path / "new.txt").exists()
    assert not runner._tool_executor._permission_engine.storage.rules_for_scope("session")
    assert any(event.tool_result and event.tool_result.error_code == "steering_superseded" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", " \n\t"])
async def test_empty_plan_is_a_recoverable_tool_error(openai_provider_config, tmp_path, content):
    provider = Provider([
        calls(("write_plan_file", {"content": content})),
        calls(("write_plan_file", {"content": "1. 完成调查后再修改"})),
        reply("计划已准备"),
    ])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WritePlanFileTool())
    runner.set_phase("plan")
    runner.set_permission_policy("bypass")

    # 两次工具调用和三次模型响应都会同步保存事件；Windows 路径校验与落盘
    # 实测可超过 3 秒。保留 10 秒死锁上限，业务结果仍由下面的断言检查。
    events = await asyncio.wait_for(collect(runner, "制定计划"), 10)

    assert not any(event.kind == "turn_failed" for event in events)
    results = [event.tool_result for event in events if event.kind == "tool_result_received"]
    assert results[0].error_code == "invalid_arguments"
    assert results[1].ok
    assert session.plan_snapshot is not None and session.plan_snapshot.ready
    assert session.plan_snapshot.content == "1. 完成调查后再修改"


@pytest.mark.asyncio
async def test_queued_turn_honors_queue_pause_when_later_item_is_paused(openai_provider_config, tmp_path):
    provider = Provider([reply("不应自动启动")])
    runner, _ = make_runner(provider, openai_provider_config, tmp_path)
    runner.enqueue_input("已排队的消息")
    runner.enqueue_input("任务结束后才送达的补充", "steer")
    assert runner.queue_paused

    assert not [event async for event in runner.run_next_queued_turn()]
    assert not provider.requests
    assert len(runner.pending_inputs) == 2


@pytest.mark.asyncio
async def test_restore_queue_saved_during_approval_closes_interrupted_tool_pair(openai_provider_config, tmp_path):
    provider = Provider([calls(("write_file", {"path": "new.txt", "content": "未批准"}))])
    runner, session = make_runner(provider, openai_provider_config, tmp_path, WriteFileTool())
    restored = SessionController(openai_provider_config, cwd=tmp_path)

    def handle(event):
        if event.kind == "permission_request_created":
            # 排队立即自动保存，此刻磁盘中只有 tool_use，还没有工具结果。
            runner.enqueue_input("下次继续检查")
            # 正在运行的会话不能被另一写入者打开，先只读捕获磁盘记录。
            from lancher_code.sessions.codec import SessionCodec
            snapshot = SessionCodec.project(session._sessions.repository.read(session.session_id))
            restored._state, restored._transcript, _, _ = SessionCodec.decode(snapshot, session.session_id)
            restored._transcript = restored._recover_interrupted_history(restored.state, restored.transcript)
            for item in restored.state.pending_inputs:
                item.state = 'paused'
            runner.cancel_active_turn()

    await asyncio.wait_for(collect(runner, "修改文件", handle), 3)
    assert restored.pending_inputs[0].state == "paused"
    assert restored.state.messages[-1].status == "cancelled"
    restored.create_user_message("检查当前状态后继续")
    request = restored.build_request([], allow_tool_calls=True)
    uses = [block.call_id for message in request.messages for block in message.blocks if block.kind == "tool_use"]
    outputs = [block for message in request.messages for block in message.blocks if block.kind == "tool_result"]
    assert [block.call_id for block in outputs] == uses
    assert outputs[-1].is_error
    assert "可能已部分执行" in outputs[-1].text
    assert not (tmp_path / "new.txt").exists()


def test_interrupted_restore_pairs_reused_call_ids_within_each_batch(openai_provider_config, tmp_path):
    session = SessionController(openai_provider_config, cwd=tmp_path)
    session.create_user_message("第一次读取")
    first = session.create_assistant_message()
    call = ToolCall(call_id="reused-id", call_index=0, tool_name="read_file",
        arguments={"path": "a.txt"}, arguments_json='{"path":"a.txt"}')
    session.append_assistant_tool_calls([call])
    session.append_tool_results([ToolExecutionResult(
        call_id=call.call_id, tool_name=call.tool_name, content="第一次已读取", is_error=False,
    )])
    session.complete_message(first.id)
    session.create_user_message("再次读取")
    session.create_assistant_message()
    session.append_assistant_tool_calls([call])
    session.update_pending_inputs([PendingInput("queued-id", "稍后继续")])

    saved_id = session.session_id
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)

    outputs = [block for message in restored.transcript for block in message.blocks if block.kind == "tool_result"]
    assert len(outputs) == 2
    assert outputs[0].text == "第一次已读取" and not outputs[0].is_error
    assert outputs[1].is_error and "可能已部分执行" in outputs[1].text
