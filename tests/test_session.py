from __future__ import annotations

from lancher_code.models import SessionState
from lancher_code.session import SessionController


def test_session_controller_appends_messages(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    controller.record_user_message("你好")
    controller.record_assistant_message("你好呀")

    assert [message.role for message in controller.state.messages] == ["user", "assistant"]
    assert [message.content for message in controller.state.messages] == ["你好", "你好呀"]


def test_session_controller_builds_request_with_history(openai_provider_config) -> None:
    controller = SessionController(
        openai_provider_config,
        state=SessionState(),
    )
    controller.record_user_message("第一轮")
    controller.record_assistant_message("第一轮回复")
    controller.record_user_message("第二轮")

    request = controller.build_request()

    assert request.model == "gpt-test"
    assert [message.content for message in request.messages] == [
        "第一轮",
        "第一轮回复",
        "第二轮",
    ]
    assert request.thinking is None


def test_session_controller_keeps_user_message_without_assistant_reply(openai_provider_config) -> None:
    controller = SessionController(openai_provider_config)

    controller.record_user_message("失败轮次消息")

    assert [message.role for message in controller.state.messages] == ["user"]
    assert controller.state.messages[0].content == "失败轮次消息"


def test_session_controller_for_claude_includes_thinking(claude_provider_config) -> None:
    controller = SessionController(claude_provider_config)
    controller.record_user_message("你好")

    request = controller.build_request()

    assert request.thinking is not None
    assert request.thinking.enabled is True
    assert request.thinking.budget_tokens == 512
