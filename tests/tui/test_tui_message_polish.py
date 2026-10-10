from __future__ import annotations

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from lancher_code.sessions.models import SessionMessage, TraceEntry
from lancher_code.agent.events import TurnEvent
from lancher_code.config.models import UIConfig
from lancher_code.tui.message import MessageWidget
from lancher_code.tui.timeline import ThinkingTraceWidget, ToolActivityWidget, ToolCallWidget
from lancher_code.tui.theme import theme_palette
from test_tui_flow import FakeProvider, _build_app


def _thought(text: str = "先检查保存入口") -> TraceEntry:
    return TraceEntry(kind="thinking", text=text, metadata={"state": "complete"})


def _tools(group: str = "first", *, state: str = "complete") -> list[TraceEntry]:
    entries = []
    for number in range(2):
        call_id = f"{group}-{number}"
        entries.extend([
            TraceEntry(
                kind="tool_call", call_id=call_id, tool_name="read_file",
                arguments={"path": f"settings_{number}.py"},
                metadata={"state": state, "group_id": group},
            ),
            TraceEntry(
                kind="tool_result", call_id=call_id, tool_name="read_file",
                ok=state == "complete", text="已读取" if state == "complete" else "调用未完成",
                metadata={"state": state, "group_id": group, "started": True,
                          "content": "文件内容" if state == "complete" else "需要用户处理的问题"},
            ),
        ])
    return entries


def _add_process(message: SessionMessage, *, state: str = "complete") -> None:
    message.trace.entries = [_thought(), *_tools(state=state)]


def _open_process(widget: MessageWidget) -> None:
    widget.query_one(ThinkingTraceWidget).set_collapsed(False)
    widget.query_one(ToolActivityWidget).set_collapsed(False)
    for call in widget.query(ToolCallWidget):
        call.set_collapsed(False)


def _assert_collapsed(widget: MessageWidget, collapsed: bool) -> None:
    assert widget.query_one(ThinkingTraceWidget).collapsed is collapsed
    assert widget.query_one(ToolActivityWidget).collapsed is collapsed
    assert all(call.collapsed is collapsed for call in widget.query(ToolCallWidget))


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("size", [(100, 40), (60, 24), (32, 16)])
async def test_message_names_align_and_timeline_spacing_has_one_rhythm(
    openai_provider_config, tmp_path, theme, size,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, UIConfig(theme=theme), tmp_path)
    user = session.create_user_message("请检查保存入口。")
    assistant = session.create_assistant_message()
    assistant.trace.entries = [
        _thought(), *_tools(), _thought("接着核对取消入口"),
        TraceEntry(kind="text", text="已找到保存入口。"),
        _thought("最后检查返回位置"), *_tools("second"),
        TraceEntry(kind="text", text="修改已经完成。"),
    ]
    async with app.run_test(size=size) as pilot:
        await app._transcript.mount_message(user)
        await app._transcript.mount_message(assistant)
        await pilot.pause()
        user_widget, assistant_widget = (app._transcript.message_widgets[message.id] for message in (user, assistant))
        labels = [widget.query_one(".message-label", Static) for widget in (user_widget, assistant_widget)]
        for label, name in zip(labels, ("你", "LanCher"), strict=True):
            assert isinstance(label.content, Text)
            assert label.content.plain == name
            style = label.content.get_style_at_offset(app.console, 0)
            assert style.bold
            assert style.color == Style.parse(theme_palette(theme)["primary"]).color

        body = user_widget.query_one(".message-body")
        text_blocks = list(assistant_widget.query(".timeline-text"))
        # 用户与助手共用左边界，不再为用户消息单独增加边线和缩进。
        assert len({label.region.x for label in labels} | {body.region.x, *(block.region.x for block in text_blocks)}) == 1
        assert user_widget.styles.border_left[0] == assistant_widget.styles.border_left[0] == ""
        assert assistant_widget.region.y - user_widget.region.bottom == 1

        blocks = list(assistant_widget.query_one(".message-timeline").children)
        assert len(blocks) == 7
        for previous, following in zip(blocks, blocks[1:]):
            process_pair = all(isinstance(block, (ThinkingTraceWidget, ToolActivityWidget))
                               for block in (previous, following))
            gap = following.region.y - previous.region.bottom
            assert gap == (0 if process_pair else 1)


@pytest.mark.asyncio
async def test_task_completion_collapses_every_segment_once_without_touching_older_history(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    # 只驱动真实事件消费者；不启动另一个后台模型任务。
    monkeypatch.setattr(app, "process_prompt", lambda *_args, **_kwargs: None)
    async with app.run_test() as pilot:
        app._begin_turn("较早的任务")
        history = session.create_assistant_message()
        _add_process(history)
        await app._consume_turn_event(TurnEvent(kind="assistant_message_started", message=history))
        history.status = "complete"
        await app._consume_turn_event(TurnEvent(kind="assistant_message_completed", message=history))
        await app._consume_turn_event(TurnEvent(kind="turn_completed"))
        historical_widget = app._transcript.message_widgets[history.id]
        _open_process(historical_widget)

        app._is_streaming = False
        app._begin_turn("当前任务")
        current = []
        for index in range(2):
            if index:
                await app._consume_turn_event(TurnEvent(kind="steering_applied"))
            message = session.create_assistant_message()
            _add_process(message)
            await app._consume_turn_event(TurnEvent(kind="assistant_message_started", message=message))
            widget = app._transcript.message_widgets[message.id]
            _open_process(widget)
            message.status = "complete"
            await app._consume_turn_event(TurnEvent(kind="assistant_message_completed", message=message))
            # 消息段结束不是整个任务完成，仍允许阅读刚展开的详情。
            _assert_collapsed(widget, False)
            current.append((message, widget))

        await app._consume_turn_event(TurnEvent(kind="turn_completed"))
        await pilot.pause()
        _assert_collapsed(historical_widget, False)
        for _, widget in current:
            _assert_collapsed(widget, True)

        # 成功后重新展开，普通刷新与重复完成事件都不能反复收起历史。
        for message, widget in current:
            _open_process(widget)
            await app._consume_turn_event(TurnEvent(kind="usage_updated", message=message))
        await app._consume_turn_event(TurnEvent(kind="turn_completed"))
        for _, widget in current:
            _assert_collapsed(widget, False)
        _assert_collapsed(historical_widget, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "event_kind", "status_label"), [
    ("error", "turn_failed", "未完成"),
    ("cancelled", "turn_cancelled", "已停止"),
])
async def test_failed_or_cancelled_task_keeps_issue_details_and_role_name_visible(
    openai_provider_config, ui_config, tmp_path, status, event_kind, status_label,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    async with app.run_test() as pilot:
        message = session.create_assistant_message()
        _add_process(message, state=status)
        await app._consume_turn_event(TurnEvent(kind="assistant_message_started", message=message))
        widget = app._transcript.message_widgets[message.id]
        _open_process(widget)
        message.status = status
        await app._consume_turn_event(TurnEvent(kind="assistant_message_completed", message=message))
        await app._consume_turn_event(TurnEvent(kind=event_kind))
        await pilot.pause()
        _assert_collapsed(widget, False)
        assert widget.query_one(ToolActivityWidget).body.display
        assert all(call.body.display for call in widget.query(ToolCallWidget))
        label = widget.query_one(".message-label", Static).content
        assert isinstance(label, Text)
        assert label.plain.startswith("LanCher")
        assert status_label in label.plain
        assert label.get_style_at_offset(app.console, 0).bold


@pytest.mark.asyncio
async def test_restored_complete_message_starts_collapsed_and_can_be_reopened(
    openai_provider_config, ui_config, tmp_path,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    message = session.create_assistant_message()
    _add_process(message)
    message.status = "complete"
    message.trace.collapsed = False
    async with app.run_test() as pilot:
        await app._transcript.mount_message(message)
        await pilot.pause()
        widget = app._transcript.message_widgets[message.id]
        _assert_collapsed(widget, True)
        _open_process(widget)
        await widget.update_from_message(message)
        _assert_collapsed(widget, False)
