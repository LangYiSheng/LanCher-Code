from __future__ import annotations

from pathlib import Path

import pytest
from provider_helpers import complete_test_response

from lancher_code.context.summary import SUMMARY_HEADINGS
from lancher_code.contracts.messages import ContentBlock, StreamEvent
from lancher_code.contracts.tools import ToolCall, ToolExecutionResult
from lancher_code.sessions.controller import SessionController
from lancher_code.usage.models import MessageUsage


class _SummaryProvider:
    @complete_test_response
    async def stream_chat(self, request):
        summary = "<summary>" + "\n".join(f"## {heading}\n已整理。" for heading in SUMMARY_HEADINGS) + "</summary>"
        yield StreamEvent(kind="text_delta", text=summary)
        yield StreamEvent(kind="message_end", usage=MessageUsage(input_tokens=100, output_tokens=50, cached_input_tokens=0), response_complete=True)


def _result_text(messages, call_id: str) -> str:
    return next(block.text for message in messages for block in message.blocks
                if block.kind == "tool_result" and block.call_id == call_id)


@pytest.mark.asyncio
async def test_offload_compaction_and_restore_keep_original_tool_result_for_each_projection(
    openai_provider_config, tmp_path: Path,
) -> None:
    session = SessionController(openai_provider_config, cwd=tmp_path)
    restored = None
    try:
        session.create_user_message("旧材料" + "x" * 50000)
        reply = session.create_assistant_message()
        session.append_message_content(reply.id, "旧材料已记录。")
        session.complete_message(reply.id)
        session.create_user_message("检查最后的错误，保留本条指令。")
        session.append_assistant_response([ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name, input=call.arguments) for call in [ToolCall(call_index=0, call_id="kept-result", tool_name="demo",
                                                    arguments={}, arguments_json="{}")]])
        original = "first line\n" + "x" * 40000 + "\nlast error line"
        session.append_tool_results([ToolExecutionResult(call_id="kept-result", tool_name="demo", content=original)])
        assert await session.offload_large_tool_results() == 1
        replacement = session.context_state.replacements["kept-result"]
        assert (tmp_path / replacement).read_text(encoding="utf-8") == original

        await session.compact_context(provider=_SummaryProvider(), visible_tools=[])
        assert _result_text(session.transcript, "kept-result") == original
        for _ in range(3):
            projected = _result_text(session.build_request([], allow_tool_calls=True).messages, "kept-result")
            assert projected.count("[工具结果已卸载]") == 1
            assert f"原始大小：{len(original.encode('utf-8'))} UTF-8 字节" in projected
            assert "first line" in projected and "last error line" in projected
        session_id = session.session_id
        session.close()

        restored = SessionController(openai_provider_config, cwd=tmp_path)
        restored.resume_session(session_id)
        assert _result_text(restored.transcript, "kept-result") == original
        projected = _result_text(restored.build_request([], allow_tool_calls=True).messages, "kept-result")
        assert projected.count("[工具结果已卸载]") == 1
        assert f"原始大小：{len(original.encode('utf-8'))} UTF-8 字节" in projected
        assert (tmp_path / replacement).read_text(encoding="utf-8") == original
    finally:
        session.close()
        if restored is not None:
            restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_both", [True, False])
async def test_compaction_restores_each_reused_call_id_by_retained_occurrence(
    openai_provider_config, tmp_path: Path, keep_both: bool,
) -> None:
    if not keep_both:
        openai_provider_config.context_window = 8192
    session = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        session.create_user_message("旧材料" + "x" * 50000)
        reply = session.create_assistant_message()
        session.append_message_content(reply.id, "旧材料已记录。")
        session.complete_message(reply.id)
        originals = ["第一次结果" + "a" * (100 if keep_both else 5000), "第二次结果" + "b" * 200]
        for index, original in enumerate(originals):
            session.create_user_message(f"第 {index + 1} 轮检查")
            session.append_assistant_response([ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name, input=call.arguments) for call in [ToolCall(call_index=0, call_id="reused-call", tool_name="demo",
                                                        arguments={}, arguments_json="{}")]])
            session.append_tool_results([ToolExecutionResult(call_id="reused-call", tool_name="demo", content=original)])

        await session.compact_context(provider=_SummaryProvider(), visible_tools=[])
        retained = [block.text for message in session.transcript for block in message.blocks
                    if block.kind == "tool_result" and block.call_id == "reused-call"]
        assert retained == (originals if keep_both else originals[-1:])
        projected = [block.text for message in session.build_request([], allow_tool_calls=True).messages
                     for block in message.blocks if block.kind == "tool_result" and block.call_id == "reused-call"]
        assert projected == retained
    finally:
        session.close()
