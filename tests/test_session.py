from __future__ import annotations

from datetime import timezone

from lancher_code.models import MessageUsage, SessionState
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


def test_session_controller_builds_request_with_history(openai_provider_config) -> None:
    controller = SessionController(
        openai_provider_config,
        state=SessionState(),
    )
    controller.create_user_message("第一轮")
    first_reply = controller.create_assistant_message()
    controller.append_message_content(first_reply.id, "第一轮回复")
    controller.complete_message(first_reply.id)
    controller.create_user_message("第二轮")

    request = controller.build_request()

    assert request.model == "gpt-test"
    assert [message.content for message in request.messages] == [
        "第一轮",
        "第一轮回复",
        "第二轮",
    ]
    assert request.thinking is None


def test_session_controller_skips_error_and_streaming_assistant_messages(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    controller.create_user_message("失败轮次消息")
    failed_message = controller.create_assistant_message()
    controller.fail_message(failed_message.id, "网络失败")
    streaming_message = controller.create_assistant_message()
    controller.append_message_content(streaming_message.id, "还没结束")
    controller.create_user_message("下一轮")

    request = controller.build_request()

    assert [message.role for message in request.messages] == ["user", "user"]
    assert [message.content for message in request.messages] == ["失败轮次消息", "下一轮"]


def test_session_controller_for_claude_includes_thinking(claude_provider_config) -> None:
    controller = SessionController(claude_provider_config)
    controller.create_user_message("你好")

    request = controller.build_request()

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
