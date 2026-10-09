from __future__ import annotations

from datetime import date, timezone
from pathlib import Path

import pytest

from lancher_code.models import (
    ContextFileSnapshot,
    MessageUsage,
    PermissionRule,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    ToolResultReplacement,
)
from lancher_code.permission_engine import PermissionStorage
from lancher_code.session import SessionController
from lancher_code.sessions.repository import SessionRepositoryError


def test_session_controller_creates_messages_with_metadata(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    user_message = controller.create_user_message("你好")
    assistant_message = controller.create_assistant_message()
    assert assistant_message.trace.collapsed is True
    controller.append_message_content(assistant_message.id, "你好呀")
    controller.complete_message(assistant_message.id, MessageUsage(input_tokens=3, output_tokens=2))

    assert [message.role for message in controller.state.messages] == ["user", "assistant"]
    assert user_message.id
    assert assistant_message.id
    assert user_message.status == "complete"
    assert assistant_message.status == "complete"
    assert assistant_message.content == "你好呀"
    assert assistant_message.usage.input_tokens == 3
    assert assistant_message.usage.cached_input_tokens == 0
    assert assistant_message.usage.output_tokens == 2
    assert user_message.timestamp.tzinfo == timezone.utc
    assert [message.role for message in controller.transcript] == ["user", "assistant"]


def test_session_controller_builds_request_with_system_messages_and_tools(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    controller.create_user_message("第一轮")
    first_reply = controller.create_assistant_message()
    controller.append_message_content(first_reply.id, "第一轮回答")
    controller.complete_message(first_reply.id)
    controller.create_user_message("第二轮")

    request = controller.build_request(
        [ToolDefinition(name="read_file", description="读取文件", input_schema={"type": "object"})],
        allow_tool_calls=True,
    )

    assert request.model == "gpt-test"
    assert request.allow_tool_calls is True
    assert request.tools[0].name == "read_file"
    assert len(request.system) == 2
    assert [message.role for message in request.messages] == ["user", "assistant", "user"]
    assert request.messages[0].blocks[0].text == "第一轮"


def test_session_controller_keeps_stable_system_prompt_between_requests(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    first_request = controller.build_request([], allow_tool_calls=True)
    second_request = controller.build_request([], allow_tool_calls=False)

    assert first_request.system[0] == second_request.system[0]
    assert first_request.system[1] == second_request.system[1]


def test_session_controller_system_and_environment_prompt_are_split(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(
        openai_provider_config,
        cwd=tmp_path,
        current_date=date(2026, 6, 28),
    )

    request = controller.build_request([], allow_tool_calls=True)
    system_prompt, environment_prompt = request.system

    assert str(tmp_path.resolve()) not in system_prompt
    assert "2026-06-28" not in system_prompt
    assert "<system-reminder>" not in system_prompt
    assert str(tmp_path.resolve()) in environment_prompt
    assert "2026-06-28" in environment_prompt
    assert "当前系统：" in environment_prompt


def test_session_controller_tracks_initial_plan_mode_prompt_and_turn_count(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.set_runtime_mode("plan")

    controller.create_user_message("计划一下")

    assert controller.state.plan_mode_turn_count == 1
    assert controller.state.pending_plan_entry_kind is None
    assert controller.transcript[0].blocks[0].text.startswith("<system-reminder>")
    assert "用户刚进入 Plan Mode" in controller.transcript[0].blocks[0].text


def test_session_controller_refreshes_full_plan_prompt_every_five_turns(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.set_runtime_mode("plan")

    for index in range(6):
        controller.create_user_message(f"第 {index + 1} 轮")

    assert controller.state.plan_mode_turn_count == 6
    assert "Plan Mode 已持续多轮" in controller.transcript[-1].blocks[0].text


def test_session_controller_injects_exit_prompt_on_first_normal_turn_after_plan_mode(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.set_runtime_mode("plan")
    controller.create_user_message("计划一下")

    controller.set_runtime_mode("default")
    assert controller.state.pending_plan_exit_notice is True

    controller.create_user_message("开始实现")

    assert "规划模式已结束" in controller.transcript[-1].blocks[0].text
    assert controller.state.pending_plan_exit_notice is False

    controller.create_user_message("继续实现")
    assert controller.transcript[-1].blocks[0].text == "继续实现"


def test_session_controller_does_not_trust_project_plan_file_as_session_snapshot(openai_provider_config, tmp_path: Path) -> None:
    plan_file = tmp_path / ".lancher" / "plan.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# plan", encoding="utf-8")
    controller = SessionController(openai_provider_config, cwd=tmp_path)

    controller.set_runtime_mode("plan")
    controller.create_user_message("继续规划")

    assert "用户刚进入 Plan Mode" in controller.transcript[0].blocks[0].text
    assert controller.plan_snapshot is None
    assert "正在重新进入 Plan Mode" not in controller.transcript[0].blocks[0].text


def test_session_controller_appends_trace_tool_calls_and_results(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    assistant_message = controller.create_assistant_message()
    controller.append_trace_thinking(assistant_message.id, "先看一下文件")
    controller.append_trace_tool_calls(
        assistant_message.id,
        [
            ToolCall(
                call_index=0,
                call_id="call-1",
                tool_name="read_file",
                arguments={"path": "demo.txt"},
                arguments_json='{"path":"demo.txt"}',
            )
        ],
    )
    controller.append_trace_tool_results(
        assistant_message.id,
        [
            ToolExecutionResult(
                call_id="call-1",
                tool_name="read_file",
                ok=True,
                payload={"content": "1: hello"},
                summary="已读取文件 demo.txt",
            )
        ],
    )

    traced_message = controller.get_message(assistant_message.id)
    entries = traced_message.trace.entries
    assert [entry.kind for entry in entries] == ["thinking", "tool_call", "tool_result"]
    assert traced_message.trace.collapsed is False

    controller.complete_message(assistant_message.id)
    assert controller.get_message(assistant_message.id).trace.collapsed is True


def test_timeline_preserves_interleaved_stream_segments_and_tool_details(openai_provider_config, tmp_path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message("开始调查")
    assistant = controller.create_assistant_message()
    controller.append_trace_thinking(assistant.id, "先调查")
    controller.append_message_content(assistant.id, "说明")
    controller.append_trace_thinking(assistant.id, "再确认")
    controller.append_trace_thinking(assistant.id, "边界")
    controller.append_trace_tool_calls(assistant.id, [ToolCall(0, "read-1", "read_file", {}, "{}")])
    controller.set_trace_tool_state(assistant.id, "read-1", "running")
    controller.append_trace_tool_results(assistant.id, [ToolExecutionResult(
        "read-1", "read_file", content="完整结果\n第二行", is_error=False, summary="读取完成",
    )])
    controller.clear_message_content(assistant.id)
    controller.append_message_content(assistant.id, "最终回答")
    controller.complete_message(assistant.id)
    saved_id = controller.session_id
    controller.close()

    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)
    message = restored.state.messages[-1]
    entries = message.trace.entries
    assert message.timeline_version == 1
    assert message.content == "最终回答"
    assert [entry.kind for entry in entries] == ["thinking", "text", "thinking", "tool_call", "tool_result", "text"]
    assert entries[2].text == "再确认边界"
    assert all(entry.metadata["state"] == "complete" for entry in entries)
    assert entries[3].metadata["group_id"] == entries[4].metadata["group_id"]
    assert entries[4].text == "读取完成"
    assert entries[4].metadata["content"] == "完整结果\n第二行"


@pytest.mark.parametrize("state", ["error", "cancelled"])
def test_timeline_direct_terminal_update_keeps_notice_and_partial_output(openai_provider_config, state) -> None:
    controller = SessionController(openai_provider_config)
    assistant = controller.create_assistant_message()
    controller.append_message_content(assistant.id, "部分输出")
    if state == "error":
        controller.fail_message(assistant.id, "连接中断")
    else:
        controller.cancel_message(assistant.id, "已停止")
    entries = assistant.trace.entries
    assert [entry.kind for entry in entries] == ["text", "notice"]
    assert entries[0].text == "部分输出"
    assert entries[0].metadata["state"] == state
    assert entries[-1].text == ("连接中断" if state == "error" else "已停止")


def test_timeline_resume_marks_unfinished_segments_and_tools_cancelled(openai_provider_config, tmp_path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message("开始调查")
    assistant = controller.create_assistant_message()
    call = ToolCall(0, "read-1", "read_file", {}, "{}")
    controller.append_trace_tool_calls(assistant.id, [call])
    controller.append_assistant_tool_calls([call])
    controller.set_trace_tool_state(assistant.id, call.call_id, "running")
    saved_id = controller.session_id
    controller.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)
    message = restored.state.messages[-1]
    assert message.status == "cancelled"
    assert message.trace.entries[0].metadata["state"] == "cancelled"
    assert message.trace.entries[1].metadata["error_code"] == "tool_result_interrupted"
    assert message.trace.entries[1].metadata["state"] == "cancelled"
    assert message.trace.entries[-1].kind == "notice"
    assert len([block for item in restored.transcript for block in item.blocks if block.kind == "tool_result"]) == 1




def test_session_controller_appends_transcript_tool_calls_and_results(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    controller.create_user_message("帮我读文件")
    controller.append_assistant_tool_calls(
        [
            ToolCall(
                call_index=0,
                call_id="call-1",
                tool_name="read_file",
                arguments={"path": "demo.txt"},
                arguments_json='{"path":"demo.txt"}',
            )
        ]
    )
    controller.append_tool_results(
        [
            ToolExecutionResult(
                call_id="call-1",
                tool_name="read_file",
                ok=True,
                payload={"content": "1: hello"},
                summary="已读取文件 demo.txt",
            )
        ]
    )

    assert [message.role for message in controller.transcript] == ["user", "assistant", "tool"]
    assert controller.transcript[1].blocks[0].kind == "tool_use"
    assert controller.transcript[2].blocks[0].kind == "tool_result"


def test_session_controller_groups_parallel_tool_results_in_one_message(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    controller.create_user_message("并行检查项目")
    controller.append_assistant_tool_calls(
        [
            ToolCall(0, "call-1", "glob", {"pattern": "*"}, '{"pattern":"*"}'),
            ToolCall(1, "call-2", "grep", {"pattern": "context"}, '{"pattern":"context"}'),
        ]
    )
    controller.append_tool_results(
        [
            ToolExecutionResult("call-1", "glob", content="找到 9 个文件", is_error=False),
            ToolExecutionResult("call-2", "grep", content="找到 436 条命中", is_error=False),
        ]
    )

    assert [message.role for message in controller.transcript] == ["user", "assistant", "tool"]
    assert [block.call_id for block in controller.transcript[2].blocks] == ["call-1", "call-2"]
    assert all(block.kind == "tool_result" for block in controller.transcript[2].blocks)


def test_session_save_and_resume_restores_ui_and_protocol_state(openai_provider_config, tmp_path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message("先检查项目")
    assistant = controller.create_assistant_message()
    controller.append_trace_thinking(assistant.id, "思考")
    controller.append_assistant_tool_calls(
        [ToolCall(0, "call-1", "read", {"path": "a.py"}, '{"path":"a.py"}')]
    )
    controller.append_tool_results(
        [ToolExecutionResult("call-1", "read", content="内容", is_error=False)]
    )
    controller.append_message_content(assistant.id, "完成")
    controller.complete_message(assistant.id, MessageUsage(input_tokens=3, output_tokens=2))
    controller.set_runtime_mode("acceptEdits")
    saved_id = controller.session_id
    controller.close()

    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)

    assert [message.content for message in restored.state.messages] == ["先检查项目", "完成"]
    assert restored.state.messages[1].trace.entries[0].text == "思考"
    assert [message.role for message in restored.transcript] == ["user", "assistant", "tool", "assistant"]
    assert restored.runtime_mode == "acceptEdits"
    assert restored.total_usage().output_tokens == 2
    assert restored.session_id == saved_id










def test_session_round_trips_context_management(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    controller.create_user_message("保存上下文")
    context = controller.context_state
    original_context_id = context.context_id
    context.seen_call_ids.add("call-1")
    context.replacements["call-1"] = ToolResultReplacement(
        call_id="call-1",
        preview="preview",
        relative_path=".lancher/context/demo/tool-results/result.txt",
        original_bytes=123,
    )
    context.recent_files.append(
        ContextFileSnapshot(
            path="demo.py",
            normalized_path=str((tmp_path / "demo.py").resolve()),
            content="print('ok')",
            read_at="2026-07-17T00:00:00+00:00",
        )
    )
    context.automatic_failure_count = 2
    saved_id = controller.session_id
    controller.close()

    restored = SessionController(openai_provider_config, cwd=tmp_path)
    restored.resume_session(saved_id)

    restored_context = restored.context_state
    assert restored_context.context_id == original_context_id
    assert restored_context.seen_call_ids == {"call-1"}
    assert restored_context.replacements["call-1"].preview == "preview"
    assert restored_context.recent_files[0].path == "demo.py"
    assert restored_context.automatic_failure_count == 2




def test_session_controller_skips_error_and_streaming_assistant_messages_from_transcript(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    controller.create_user_message("失败轮次消息")
    failed_message = controller.create_assistant_message()
    controller.fail_message(failed_message.id, "网络失败")
    streaming_message = controller.create_assistant_message()
    controller.append_message_content(streaming_message.id, "还没结束")
    controller.create_user_message("下一轮")

    request = controller.build_request([], allow_tool_calls=True)

    assert [message.role for message in request.messages] == ["user", "user"]
    assert [message.blocks[-1].text for message in request.messages] == ["失败轮次消息", "下一轮"]


def test_session_controller_for_claude_includes_thinking(claude_provider_config) -> None:
    controller = SessionController(claude_provider_config)
    controller.create_user_message("你好")

    request = controller.build_request([], allow_tool_calls=True)

    assert request.thinking is not None
    assert request.thinking.enabled is True
    assert request.thinking.budget_tokens == 512


def test_session_controller_totals_usage(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    controller.create_user_message("你好")
    assistant_message = controller.create_assistant_message()
    controller.complete_message(assistant_message.id, MessageUsage(input_tokens=5, output_tokens=7))

    usage = controller.total_usage()

    assert usage.input_tokens == 5
    assert usage.cached_input_tokens == 0
    assert usage.output_tokens == 7


def test_session_controller_can_accumulate_usage_before_completion(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    assistant_message = controller.create_assistant_message()

    controller.add_message_usage(assistant_message.id, MessageUsage(input_tokens=3, cached_input_tokens=1, output_tokens=4))
    controller.add_message_usage(assistant_message.id, MessageUsage(input_tokens=2, cached_input_tokens=2, output_tokens=1))

    usage = controller.total_usage()

    assert usage.input_tokens == 5
    assert usage.cached_input_tokens == 3
    assert usage.output_tokens == 5


def test_session_controller_keeps_usage_when_message_fails(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    assistant_message = controller.create_assistant_message()
    controller.add_message_usage(assistant_message.id, MessageUsage(input_tokens=8, cached_input_tokens=5, output_tokens=13))

    controller.fail_message(assistant_message.id, "网络失败")

    failed = controller.get_message(assistant_message.id)
    assert failed.usage.input_tokens == 8
    assert failed.usage.cached_input_tokens == 5
    assert failed.usage.output_tokens == 13
