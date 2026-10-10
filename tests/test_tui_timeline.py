from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from textual.widgets import Static

from lancher_code.models import ToolExecutionResult, TraceEntry, TurnEvent, UIConfig
from lancher_code.tui_views.message import ThinkingTraceWidget, ToolActivityWidget, ToolCallWidget
from lancher_code.tui_views.theme import theme_palette
from lancher_code.tui_views.timeline import _thinking_display_parts, timeline_blocks
from test_tui_flow import FakeProvider, _build_app


def _segment(kind: str, text: str, *, state: str = "complete") -> TraceEntry:
    return TraceEntry(kind=kind, text=text, metadata={"state": state})


def _call(call_id: str, *, state: str = "running", group: str = "first", path: str = "settings.py") -> TraceEntry:
    return TraceEntry(
        kind="tool_call", call_id=call_id, tool_name="read_file", arguments={"path": path},
        metadata={"group_id": group, "state": state},
    )


def _result(call_id: str, *, state: str = "complete", group: str = "first", content: str = "完整文件内容") -> TraceEntry:
    ok = state == "complete"
    return TraceEntry(
        kind="tool_result", call_id=call_id, tool_name="read_file",
        text="已读取文件" if ok else "文件读取失败", ok=ok,
        metadata={"group_id": group, "state": state, "content": content,
                  "error_code": "" if ok else "read_failed"},
    )


def _header(widget) -> Static:
    # 只取自己的标题，避免把单条调用的标题当成父分组标题。
    return next(child for child in widget.children if child.has_class("trace-header"))


def _text(widget: Static) -> str:
    output = StringIO()
    Console(file=output, width=140, color_system=None).print(widget.content)
    return output.getvalue()


@pytest.mark.parametrize("raw", ["", " \t ", "\r\n\r\n", "\n  \n\t\r"])
def test_blank_thinking_keeps_raw_trace_without_empty_timeline_row(openai_provider_config, tmp_path, raw):
    _, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    entry = _segment("thinking", raw, state="streaming")
    message.trace.entries = [entry]
    assert timeline_blocks(message) == []
    assert message.trace.entries == [entry]
    assert entry.text == raw
    assert _thinking_display_parts([entry]) == ("", "")


@pytest.mark.parametrize("raw, first, remaining", [
    ("\r\n\t\r\n  先检查\t保存入口 \r\n\r\n\t\r\n再查看调用方\r\n", "先检查 保存入口", "再查看调用方"),
    ("\r  第一行\r\r第二行\r\r第三行\r", "第一行", "第二行\n\n第三行"),
    ("\n\n第一行\n  \n    保留正文缩进\n\n第二段", "第一行", "    保留正文缩进\n\n第二段"),
    ("  只有一行\t摘要  ", "只有一行 摘要", ""),
])
def test_thinking_summary_uses_first_readable_line_and_removes_boundary_blank_lines(raw, first, remaining):
    entry = _segment("thinking", raw)
    assert _thinking_display_parts([entry]) == (first, remaining)
    assert entry.text == raw


@pytest.mark.asyncio
async def test_blank_streaming_thinking_appears_only_after_readable_content(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_segment("thinking", "\r\n\t\r\n", state="streaming")]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        widget = app._message_widgets[message.id]
        assert not widget.query(ThinkingTraceWidget)
        message.trace.entries[0].text += "首个可读内容\r\n\r\n下一段内容"
        await widget.update_from_message(message)
        await pilot.pause()
        thinking = widget.query_one(ThinkingTraceWidget)
        assert not thinking.collapsed
        assert _header(thinking).content.plain == "▾ 首个可读内容"
        assert thinking.body.content.plain == "下一段内容"
        assert thinking.body.region.y == _header(thinking).region.bottom


@pytest.mark.asyncio
@pytest.mark.parametrize("call_count", [1, 2])
async def test_completion_collapses_process_and_call_details_once(openai_provider_config, tmp_path, call_count):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_segment("thinking", "先检查保存入口\n查看调用方")]
    for index in range(call_count):
        message.trace.entries.extend([_call(str(index), state="complete"), _result(str(index))])
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        thinking = widget.query_one(ThinkingTraceWidget)
        group = widget.query_one(ToolActivityWidget)
        calls = list(group.query(ToolCallWidget))
        thinking.set_collapsed(False)
        for call in calls:
            call.set_collapsed(False)
        assert not thinking.collapsed and not group.collapsed
        assert all(not call.collapsed for call in calls)

        thinking.collapse_for_completion()
        group.collapse_for_completion()
        assert thinking.collapsed and group.collapsed
        assert all(call.collapsed and not call.body.display for call in calls)

        # 完成后的刷新不会因旧流式标记重新展开，也不会干预随后手动阅读。
        message.trace.entries[0].metadata["state"] = "streaming"
        await widget.update_from_message(message)
        assert thinking.collapsed and group.collapsed
        thinking.set_collapsed(False)
        group.set_collapsed(False)
        calls[0].set_collapsed(False)
        thinking.collapse_for_completion()
        group.collapse_for_completion()
        await widget.update_from_message(message)
        assert not thinking.collapsed and not group.collapsed
        assert not calls[0].collapsed and calls[0].body.display


@pytest.mark.asyncio
async def test_timeline_preserves_thinking_text_and_tool_group_order(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.content = "中间说明最终总结"
    message.trace.entries = [
        _segment("thinking", "先查看保存入口"),
        _call("a", state="complete"), _result("a"),
        _segment("text", "中间说明"),
        _segment("thinking", "再核对取消行为"),
        _call("b", state="complete", group="second"), _result("b", group="second"),
        _segment("text", "最终总结"),
    ]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        blocks = list(widget.query_one(".message-timeline").children)
        assert len(blocks) == 6
        assert isinstance(blocks[0], ThinkingTraceWidget)
        assert isinstance(blocks[1], ToolActivityWidget)
        assert _text(blocks[2]).strip() == "中间说明"
        assert isinstance(blocks[3], ThinkingTraceWidget)
        assert isinstance(blocks[4], ToolActivityWidget)
        assert _text(blocks[5]).strip() == "最终总结"
        assert "先查看" in _text(_header(blocks[0]))
        assert "再核对" in _text(_header(blocks[3]))
        # 正文已在时间线上，不能再次追加 message.content。
        assert len(widget.query(".timeline-text")) == 2
        assert not widget.query_one(".message-body").display


@pytest.mark.asyncio
async def test_thinking_follows_stream_completion_and_preserves_manual_choice(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_segment("thinking", "先检查保存入口\n再查看调用方", state="streaming")]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        widget = app._message_widgets[message.id]
        thinking = widget.query_one(ThinkingTraceWidget)
        assert not thinking.collapsed
        assert "思考" not in _text(_header(thinking))
        message.trace.entries[0].metadata["state"] = "complete"
        await widget.update_from_message(message)
        await pilot.pause()
        assert thinking.collapsed
        assert "先检查保存入口" in _text(_header(thinking))
        assert "再查看调用方" not in _text(_header(thinking))
        await pilot.click(_header(thinking))
        assert not thinking.collapsed

        message.trace.entries.append(_segment("thinking", "第二段仍在输出", state="streaming"))
        await widget.update_from_message(message)
        await pilot.pause()
        first, second = widget.query(ThinkingTraceWidget)
        assert first is thinking
        assert not first.collapsed
        assert not second.collapsed
        _header(second).focus()
        await pilot.press("enter")
        assert second.collapsed
        message.trace.entries[-1].text += "，收到下一段增量"
        await widget.update_from_message(message)
        assert second.collapsed
        assert not first.collapsed
        message.trace.entries[-1].metadata["state"] = "complete"
        await widget.update_from_message(message)
        assert second.collapsed
        _header(second).focus()
        await pilot.press("space")
        assert not second.collapsed


@pytest.mark.asyncio
async def test_parallel_calls_have_independent_details_and_group_completion(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_call("a"), _call("b", path="models.py")]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        group = widget.query_one(ToolActivityWidget)
        a, b = group.query(ToolCallWidget)
        assert not group.collapsed
        assert a.display and b.display
        assert a.call_id == "a" and b.call_id == "b"
        await pilot.click(_header(a))
        await pilot.click(_header(b))
        assert not a.collapsed and not b.collapsed
        assert not group.collapsed
        assert a.query_one(".tool-call-body").display
        assert b.query_one(".tool-call-body").display

        message.trace.entries[0].metadata["state"] = "complete"
        message.trace.entries.append(_result("a", content="第一个调用的完整结果"))
        await widget.update_from_message(message)
        assert not group.collapsed
        assert "完成" in _text(_header(a))
        assert any(word in _text(_header(b)) for word in ("执行", "运行"))
        assert "第一个调用的完整结果" in _text(a.query_one(".tool-call-body", Static))
        assert not b.collapsed

        message.trace.entries[1].metadata["state"] = "complete"
        message.trace.entries.append(_result("b"))
        await widget.update_from_message(message)
        # 用户正在读单条详情，整组完成不能把阅读内容藏起来。
        assert not group.collapsed
        assert "已执行 2 个工具" in _text(_header(group))
        _header(group).focus()
        await pilot.press("enter")
        assert group.collapsed
        assert not a.collapsed and not b.collapsed
        await pilot.press("enter")
        assert not group.collapsed
        # 展开整组时，已手动打开的单条详情仍在。
        assert not a.collapsed and not b.collapsed
        await pilot.click(_header(a))
        assert a.collapsed and not b.collapsed
        assert not group.collapsed


@pytest.mark.asyncio
async def test_tool_group_auto_collapses_when_no_detail_is_being_read(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_call("a"), _call("b")]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        group = widget.query_one(ToolActivityWidget)
        assert not group.collapsed
        assert all(call.collapsed for call in group.query(ToolCallWidget))
        message.trace.entries[0].metadata["state"] = "complete"
        message.trace.entries.append(_result("a"))
        await widget.update_from_message(message)
        assert not group.collapsed
        message.trace.entries[1].metadata["state"] = "complete"
        message.trace.entries.append(_result("b"))
        await widget.update_from_message(message)
        assert group.collapsed
        assert "已执行 2 个工具" in _text(_header(group))


@pytest.mark.asyncio
async def test_rejected_calls_are_visible_but_not_counted_as_executed(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    rejected = _result("a", state="error", content="当前阶段禁止此操作")
    rejected.metadata["started"] = False
    denied = _result("b", state="error", content="用户拒绝了本次操作")
    denied.metadata["started"] = False
    message.trace.entries = [_call("a", state="error"), _call("b", state="error"), rejected, denied]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        group = app._message_widgets[message.id].query_one(ToolActivityWidget)
        assert not group.collapsed
        assert "已执行 0 个工具" in _text(_header(group))
        for call in group.query(ToolCallWidget):
            assert "未执行" in _text(_header(call))
            assert not call.collapsed
            assert call.query_one(".tool-call-body").display
        a, b = group.query(ToolCallWidget)
        assert "当前阶段禁止" in _text(a.query_one(".tool-call-body", Static))
        assert "用户拒绝" in _text(b.query_one(".tool-call-body", Static))


@pytest.mark.asyncio
async def test_live_ui_settings_repaint_timeline_without_losing_reading_state(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(theme="dark"), tmp_path)
    message = session.create_assistant_message()
    message.content = "最后的正文"
    message.trace.entries = [
        _segment("thinking", "先检查配置关系"),
        _call("a", state="complete"), _result("a"),
        _segment("text", "最后的正文"),
    ]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        thinking = widget.query_one(ThinkingTraceWidget)
        call = widget.query_one(ToolCallWidget)
        _header(call).focus()
        await pilot.press("enter")
        assert not call.collapsed
        assert _header(call).content.style == theme_palette("dark")["muted"]

        app._apply_ui_settings(UIConfig(theme="light", show_thinking_status=False))
        await pilot.pause()
        assert app.theme == "lancher-light"
        assert widget.query_one(ThinkingTraceWidget) is thinking
        assert widget.query_one(ToolCallWidget) is call
        assert not thinking.display
        assert not call.collapsed
        assert call.query_one(".tool-call-body").display
        assert _header(call).content.style == theme_palette("light")["muted"]
        assert _header(thinking).content.style == theme_palette("light")["muted"]
        assert len(widget.query(".timeline-text")) == 1
        assert not widget.query_one(".message-body").display
        assert "最后的正文" in _text(widget.query_one(".timeline-text", Static))

        app._apply_ui_settings(UIConfig(theme="dark", show_thinking_status=True))
        await pilot.pause()
        assert thinking.display
        assert widget.query_one(ToolCallWidget) is call
        assert not call.collapsed
        assert _header(call).content.style == theme_palette("dark")["muted"]
        assert len(widget.query(".timeline-text")) == 1


@pytest.mark.asyncio
async def test_single_tool_uses_one_disclosure_and_contains_complete_result(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_call("a", state="complete"), _result("a", content="超出摘要范围的正文\n最后一行")]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        group = app._message_widgets[message.id].query_one(ToolActivityWidget)
        call = group.query_one(ToolCallWidget)
        visible_headers = [header for header in group.query(".trace-header") if header.display]
        assert visible_headers == [_header(call)]
        assert call.collapsed
        _header(call).focus()
        await pilot.press("enter")
        assert not call.collapsed
        assert "最后一行" in _text(call.query_one(".tool-call-body", Static))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["error", "awaiting_permission", "unknown"])
async def test_failure_and_approval_remain_visible_without_expanding_group(openai_provider_config, tmp_path, state):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_call("a", state="complete"), _call("b", state=state), _result("a")]
    if state == "error":
        message.trace.entries.append(_result("b", state="error", content="拒绝读取：文件不存在"))
    elif state == "unknown":
        result = _result("b", state="unknown", content="远端结果未知，请先检查实际状态，勿重复执行")
        result.metadata.update(started=True, error_code="mcp_outcome_unknown")
        message.trace.entries.append(result)
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        group = app._message_widgets[message.id].query_one(ToolActivityWidget)
        call = next(call for call in group.query(ToolCallWidget) if call.call_id == "b")
        assert not group.collapsed
        assert call.display
        status = _text(_header(call))
        expected = {"error": ("失败", "错误"), "awaiting_permission": ("确认", "批准", "审批"), "unknown": ("结果未知",)}
        assert any(word in status for word in expected[state])
        if state == "error":
            assert not call.collapsed
            assert "文件不存在" in _text(call.query_one(".tool-call-body", Static))
        elif state == "unknown":
            assert not call.collapsed
            assert "1 项结果未知" in _text(_header(group))
            assert "勿重复执行" in _text(call.query_one(".tool-call-body", Static))


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_timeline_keyboard_details_fit_theme_and_narrow_terminal(openai_provider_config, tmp_path, theme, size):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(theme=theme), tmp_path)
    message = session.create_assistant_message()
    long_path = "目录/" + "很长的文件名称/" * 12 + "settings.py"
    message.trace.entries = [
        _segment("thinking", "先检查路径，保留整条参数与输出。"),
        _call("a", state="complete", path=long_path),
        _result("a", content="第一行\n" + "日志内容\n" * 10 + "结果末尾"),
        _segment("text", "最终正文。"),
    ]
    async with app.run_test(size=size) as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        call = widget.query_one(ToolCallWidget)
        header = _header(call)
        header.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not call.collapsed
        body = call.query_one(".tool-call-body", Static)
        detail_text = _text(body)
        # Console 会按列折行；除去空白后仍能找到完整路径和末尾结果。
        assert "".join(long_path.split()) in "".join(detail_text.split())
        assert "结果末尾" in detail_text
        assert header.region.right <= size[0]
        assert body.region.right <= size[0]
        assert app.query_one("#composer").region.bottom <= size[1]
        assert app.query_one("#status-bar").region.bottom <= size[1]
        assert app.screen.styles.background.hex.lower() == theme_palette(theme)["background"]
        header.focus()
        await pilot.press("space")
        assert call.collapsed


@pytest.mark.asyncio
async def test_new_timeline_blocks_do_not_pull_user_from_history(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    async with app.run_test(size=(60, 24)) as pilot:
        for index in range(15):
            await app._mount_message_widget(session.create_user_message(f"历史消息 {index}\n第二行"))
        message = session.create_assistant_message()
        message.trace.entries = [_segment("thinking", "正在检查", state="streaming")]
        await app._mount_message_widget(message)
        await pilot.pause()
        view = app.query_one("#chat-view")
        view.scroll_home(animate=False)
        await pilot.pause()
        assert not view.is_vertical_scroll_end
        message.trace.entries[0].metadata["state"] = "complete"
        message.trace.entries.extend([_call("a"), _call("b")])
        await app._consume_turn_event(TurnEvent(kind="tool_call_started", message=message))
        await pilot.pause()
        assert view.scroll_y == 0
        group = app._message_widgets[message.id].query_one(ToolActivityWidget)
        assert len(group.query(ToolCallWidget)) == 2


@pytest.mark.asyncio
async def test_legacy_trace_keeps_available_order_and_final_body(openai_provider_config, tmp_path):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.timeline_version = 0
    message.content = "旧会话的最终回答"
    message.trace.entries = [_segment("thinking", "旧思考"), _segment("text", "旧过程说明")]
    async with app.run_test() as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        blocks = list(app._message_widgets[message.id].query_one(".message-timeline").children)
        assert isinstance(blocks[0], ThinkingTraceWidget)
        assert "旧过程说明" in _text(blocks[1])
        assert "旧会话的最终回答" in _text(app._message_widgets[message.id].query_one(".message-body", Static))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_resource_wait_shows_approval_and_actual_blocker(openai_provider_config, tmp_path, size):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    process_id = "87654321876543218765432187654321"
    call = _call("waiting", state="waiting_resources")
    call.metadata["started"] = False
    call.metadata["waiting"] = {"reason": "resource_conflict", "blockers": [{
        "session_id": "12345678123442348123456781234567", "invocation_id": "server-call",
        "tool_name": "run_command", "process_id": process_id,
        "resources": [{"kind": "project", "key": str(tmp_path), "mode": "exclusive",
                       "recursive": True, "lifetime": "process"}],
    }]}
    message.trace.entries = [call, _call("finished", state="complete"), _result("finished")]
    async with app.run_test(size=size) as pilot:
        await app._mount_message_widget(message)
        await pilot.pause()
        widget = app._message_widgets[message.id]
        group = widget.query_one(ToolActivityWidget)
        waiting = next(item for item in widget.query(ToolCallWidget) if item.call_id == "waiting")
        assert waiting.state == "waiting_resources"
        assert "等待资源" in _text(_header(waiting))
        assert "待批准" not in _text(_header(waiting))
        assert "1 项等待资源" in _text(_header(group))
        assert not waiting.collapsed
        details = waiting.body.content.plain
        assert "审批已通过" in details and "尚未开始执行" in details
        assert process_id in details and f"/tasks show {process_id}" in details
        assert "项目" in details and "进程运行期间" in details
        assert _header(waiting).region.right <= size[0]
        assert app.query_one("#composer").region.bottom <= size[1]


def test_waiting_trace_copies_snapshot_and_clears_it_at_start_or_result(openai_provider_config, tmp_path):
    _, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    message.trace.entries = [_call("a", state="queued")]
    snapshot = {"reason": "capacity", "limit": 8, "blockers": []}
    session.set_trace_tool_state(message.id, "a", "waiting_resources", waiting=snapshot)
    snapshot["blockers"].append({"process_id": "later-change"})
    call = message.trace.entries[0]
    assert call.metadata["waiting"]["blockers"] == []
    assert not call.metadata.get("started")
    session.set_trace_tool_state(message.id, "a", "running")
    assert call.metadata["started"] is True
    assert "waiting" not in call.metadata
    session.set_trace_tool_state(message.id, "a", "waiting_resources", waiting=snapshot)
    session.append_trace_tool_results(message.id, [ToolExecutionResult(
        call_id="a", tool_name="read_file", summary="停止", content="停止", is_error=True,
        error_code="tool_result_interrupted")])
    assert "waiting" not in call.metadata


@pytest.mark.parametrize("transition", ["cancel", "recover"])
def test_resource_wait_closes_at_round_cancel_or_interrupted_restore(
    openai_provider_config, tmp_path, transition,
):
    _, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(), tmp_path)
    message = session.create_assistant_message()
    call = _call("waiting", state="waiting_resources")
    call.metadata.update(started=False, waiting={"reason": "resource_conflict", "blockers": []})
    message.trace.entries = [call]
    if transition == "cancel":
        session.cancel_message(message.id)
    else:
        session._recover_interrupted_history(session.state, session.transcript)
    assert call.metadata["state"] == "cancelled"
    assert call.metadata["started"] is False
    assert "waiting" not in call.metadata
