from __future__ import annotations

import pytest

from lancher_code.sessions.controller import SessionController


@pytest.mark.parametrize("status", ["error", "cancelled"])
def test_terminal_notice_preserves_body_and_records_its_own_severity(openai_provider_config, status):
    session = SessionController(openai_provider_config)
    message = session.create_assistant_message()
    session.append_message_content(message.id, "已经写出的 **部分回答**。")
    session.append_trace_notice(message.id, "正在自动续接。", severity="warning", code="output_limit")
    if status == "error":
        session.fail_message(message.id, "连接中断。")
    else:
        session.cancel_message(message.id, "已停止。")
    assert message.content == "已经写出的 **部分回答**。"
    notices = [entry for entry in message.trace.entries if entry.kind == "notice"]
    assert notices[0].metadata == {"severity": "warning", "code": "output_limit"}
    assert notices[1].metadata == {
        "severity": "error" if status == "error" else "info",
        "code": "turn_failed" if status == "error" else "turn_cancelled",
    }


def test_notice_deduplication_keeps_new_event_details(openai_provider_config):
    session = SessionController(openai_provider_config)
    message = session.create_assistant_message()
    session.append_trace_notice(message.id, "继续生成。", code="output_limit")
    session.append_trace_thinking(message.id, "续接后的思考")
    session.append_trace_notice(message.id, "继续生成。", code="output_limit")
    session.append_trace_notice(message.id, "已连续达到上限。", code="output_limit")
    session.append_trace_notice(message.id, "继续生成。", code="another_reason")
    notices = [entry for entry in message.trace.entries if entry.kind == "notice"]
    assert [(entry.text, entry.metadata["code"]) for entry in notices] == [
        ("继续生成。", "output_limit"), ("已连续达到上限。", "output_limit"),
        ("继续生成。", "another_reason"),
    ]


def test_failure_promotes_matching_notice_instead_of_duplicating_it(openai_provider_config):
    session = SessionController(openai_provider_config)
    message = session.create_assistant_message()
    session.append_trace_notice(message.id, "请求中断。")
    session.append_trace_thinking(message.id, "部分思考")
    session.fail_message(message.id, "请求中断。")
    notices = [entry for entry in message.trace.entries if entry.kind == "notice"]
    assert len(notices) == 1
    assert notices[0].metadata == {"severity": "error", "code": "turn_failed"}


def test_notice_severity_and_partial_body_survive_session_restore(openai_provider_config, tmp_path):
    session = SessionController(openai_provider_config, cwd=tmp_path)
    session.create_user_message("请继续输出")
    message = session.create_assistant_message()
    session.append_message_content(message.id, "已经生成的内容")
    session.append_trace_notice(message.id, "输出自动续接。", code="output_limit")
    session.fail_message(message.id, "续接连接失败。")
    session_id = session.session_id
    session.close()
    restored = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        restored.resume_session(session_id)
        saved = restored.state.messages[-1]
        assert saved.status == "error"
        assert saved.content == "已经生成的内容"
        assert [entry.metadata for entry in saved.trace.entries if entry.kind == "notice"] == [
            {"severity": "warning", "code": "output_limit"},
            {"severity": "error", "code": "turn_failed"},
        ]
    finally:
        restored.close()


def test_invalid_notice_severity_does_not_mutate_output(openai_provider_config):
    session = SessionController(openai_provider_config)
    message = session.create_assistant_message()
    session.append_trace_thinking(message.id, "仍在思考")
    with pytest.raises(ValueError, match="提示级别"):
        session.append_trace_notice(message.id, "无效提示", severity="unknown")
    assert len(message.trace.entries) == 1
    assert message.trace.entries[0].metadata["state"] == "streaming"
