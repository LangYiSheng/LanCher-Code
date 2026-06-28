from __future__ import annotations

from datetime import date
from datetime import timezone
from pathlib import Path

from lancher_code.models import MessageUsage, ToolCall, ToolDefinition, ToolExecutionResult
from lancher_code.session import SessionController


def test_session_controller_creates_messages_with_metadata(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    user_message = controller.create_user_message("你好")
    assistant_message = controller.create_assistant_message()
    controller.append_message_content(assistant_message.id, "你好呀")
    controller.complete_message(assistant_message.id, MessageUsage(input_tokens=3, output_tokens=2))

    assert [message.role for message in controller.state.messages] == ["user", "assistant"]
    assert user_message.id
    assert assistant_message.id
    assert user_message.status == "complete"
    assert assistant_message.status == "complete"
    assert assistant_message.content == "你好呀"
    assert assistant_message.usage.input_tokens == 3
    assert assistant_message.usage.output_tokens == 2
    assert user_message.timestamp.tzinfo == timezone.utc
    assert [message.role for message in controller.transcript] == ["system", "user", "assistant"]


def test_session_controller_builds_request_with_fixed_system_prompt_and_tools(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    controller.create_user_message("第一轮")
    first_reply = controller.create_assistant_message()
    controller.append_message_content(first_reply.id, "第一轮回复")
    controller.complete_message(first_reply.id)
    controller.create_user_message("第二轮")

    request = controller.build_request(
        [ToolDefinition(name="read_file", description="读取文件", input_schema={"type": "object"})],
        allow_tool_calls=True,
    )

    assert request.model == "gpt-test"
    assert request.allow_tool_calls is True
    assert request.tools[0].name == "read_file"
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "user"]
    assert request.messages[0].blocks[0].text == controller.transcript[0].blocks[0].text


def test_session_controller_does_not_replace_system_prompt_between_requests(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)
    first_request = controller.build_request([], allow_tool_calls=True)
    second_request = controller.build_request([], allow_tool_calls=False)

    assert first_request.messages[0].blocks[0].text == second_request.messages[0].blocks[0].text


def test_session_controller_system_prompt_contains_cwd_and_date(openai_provider_config, tmp_path: Path) -> None:
    controller = SessionController(
        openai_provider_config,
        cwd=tmp_path,
        current_date=date(2026, 6, 28),
    )

    system_text = controller.transcript[0].blocks[0].text

    assert str(tmp_path.resolve()) in system_text
    assert "2026-06-28" in system_text


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

    entries = controller.get_message(assistant_message.id).trace.entries
    assert [entry.kind for entry in entries] == ["thinking", "tool_call", "tool_result"]


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

    assert [message.role for message in controller.transcript] == ["system", "user", "assistant", "tool"]
    assert controller.transcript[2].blocks[0].kind == "tool_use"
    assert controller.transcript[3].blocks[0].kind == "tool_result"


def test_session_controller_skips_error_and_streaming_assistant_messages_from_transcript(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    controller.create_user_message("失败轮次消息")
    failed_message = controller.create_assistant_message()
    controller.fail_message(failed_message.id, "网络失败")
    streaming_message = controller.create_assistant_message()
    controller.append_message_content(streaming_message.id, "还没结束")
    controller.create_user_message("下一轮")

    request = controller.build_request([], allow_tool_calls=True)

    assert [message.role for message in request.messages] == ["system", "user", "user"]
    assert [message.blocks[0].text for message in request.messages[1:]] == ["失败轮次消息", "下一轮"]


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
    assert usage.output_tokens == 7
