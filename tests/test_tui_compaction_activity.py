"""压缩记录通过真实聊天事件显示、停止，并按原位置恢复。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from io import StringIO

import pytest
from rich.console import Console
from textual.containers import VerticalScroll

from lancher_code.models import ContextCompactionResult, StreamEvent, TurnEvent
from lancher_code.sessions.repository import SessionRepositoryError
from lancher_code.tui_views.compaction import CompactionActivityWidget
from lancher_code.tui_views.composer import ComposerTextArea
from lancher_code.tui_views.message import MessageWidget
from test_tui_flow import FakeProvider, _build_app, _submit_message


def _summary() -> str:
    headings = (
        "主要请求和意图", "关键技术概念", "文件和代码段", "错误与修复", "问题解决过程",
        "用户消息与明确反馈", "待办任务", "当前工作", "可能的下一步",
    )
    return "<summary>" + "\n".join(f"## {heading}\n保留任务要点" for heading in headings) + "</summary>"


def _history(session):
    session.create_user_message("分析文件")
    message = session.create_assistant_message()
    session.append_message_content(message.id, "已完成文件分析，记录修复原因与验证结果。\n" * 1_500)
    session.complete_message(message.id)
    session.create_user_message("继续下一项")


async def _until(pilot, predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await pilot.pause(0.01)


def _rendered_text(widget) -> str:
    output = StringIO()
    Console(file=output, width=140, color_system=None).print(widget.content)
    return output.getvalue()


class GateSummaryProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_chat(self, request):
        self.started.set()
        await self.release.wait()
        yield StreamEvent(kind="text_delta", text=_summary())
        yield StreamEvent(kind="message_end")


async def test_manual_compaction_updates_one_inline_row_and_preserves_expansion(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    provider = GateSummaryProvider()
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)
    _history(session)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await app._restore_session_view()
            await _submit_message(app, pilot, "/compact")
            await _until(pilot, provider.started.is_set)
            widget = app.query_one(CompactionActivityWidget)
            activity_id = widget.activity_id
            assert widget.activity.status == "running"
            assert len(session.state.messages) == 3
            widget.header.focus()
            await pilot.press("enter")
            assert not widget.collapsed
            provider.release.set()
            await _until(pilot, lambda: not app._is_streaming)
            activity = session.state.compaction_activities[activity_id]
            assert activity.status == "completed"
            assert activity.after_tokens < activity.before_tokens
            assert app.query_one(CompactionActivityWidget) is widget
            assert not widget.collapsed
            assert "已压缩上下文" in str(widget.header.render())
            assert not notifications
            assert not app.query_one(ComposerTextArea).disabled
    finally:
        await app._turn_runner.shutdown()
        session.close()


async def test_cancel_before_manual_worker_enters_stops_the_already_visible_activity(
    openai_provider_config, ui_config, tmp_path,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    try:
        async with app.run_test() as pilot:
            await app._dispatch_slash_command("compact", "")
            widget = app.query_one(CompactionActivityWidget)
            assert widget.activity.status == "running"
            await app.action_request_quit()
            await _until(pilot, lambda: not app._is_streaming)
            assert widget.activity.status == "cancelled"
            assert session.state.compaction_activities[widget.activity_id].status == "cancelled"
            assert "压缩已停止" in str(widget.header.render())
            assert not app._exit_flow.is_armed
            assert not app.query_one(ComposerTextArea).disabled
    finally:
        await app._turn_runner.shutdown()
        session.close()


async def test_cancel_status_save_failure_still_stops_spinner_and_unlocks_input(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    session.create_user_message("已有会话")
    original_flush = session.flush

    def unavailable_storage(*args, **kwargs):
        raise SessionRepositoryError("磁盘暂时不可写")

    try:
        async with app.run_test() as pilot:
            await app._dispatch_slash_command("compact", "")
            widget = app.query_one(CompactionActivityWidget)
            monkeypatch.setattr(session, "flush", unavailable_storage)
            await app.action_request_quit()
            await _until(pilot, lambda: not app._is_streaming)
            assert widget.activity.status == "cancelled"
            assert widget._spinner_timer is None
            assert app._compaction_worker is None
            assert not app.query_one(ComposerTextArea).disabled
            monkeypatch.setattr(session, "flush", original_flush)
    finally:
        monkeypatch.setattr(session, "flush", original_flush)
        await app._turn_runner.shutdown()
        session.close()


async def test_interrupt_during_activity_mount_cancels_preparation_instead_of_arming_exit(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    provider = GateSummaryProvider()
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)
    _history(session)
    mounting = asyncio.Event()
    release_mount = asyncio.Event()
    original_mount = app._mount_compaction_widget

    async def slow_mount(activity):
        mounting.set()
        await release_mount.wait()
        await original_mount(activity)

    monkeypatch.setattr(app, "_mount_compaction_widget", slow_mount)
    try:
        async with app.run_test() as pilot:
            dispatch = asyncio.create_task(app._dispatch_slash_command("compact", ""))
            await mounting.wait()
            await app.action_request_quit()
            assert not app._exit_flow.is_armed
            release_mount.set()
            await dispatch
            await pilot.pause()
            widget = app.query_one(CompactionActivityWidget)
            assert widget.activity.status == "cancelled"
            assert widget._spinner_timer is None
            assert app._compaction_worker is None
            assert not app._is_streaming
            assert not app.query_one(ComposerTextArea).disabled
            assert not provider.started.is_set()
    finally:
        await app._turn_runner.shutdown()
        session.close()


async def test_start_save_failure_is_visible_as_a_failed_chat_activity(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    session.create_user_message("已有会话")
    original_flush = session.flush

    def unavailable_storage(*args, **kwargs):
        raise SessionRepositoryError("保存启动记录失败")

    try:
        async with app.run_test() as pilot:
            monkeypatch.setattr(session, "flush", unavailable_storage)
            with pytest.raises(SessionRepositoryError, match="保存启动记录失败"):
                await app._dispatch_slash_command("compact", "")
            widget = app.query_one(CompactionActivityWidget)
            assert widget.activity.status == "failed"
            assert widget._spinner_timer is None
            assert widget.activity.error_text == "保存启动记录失败"
            assert not app._is_streaming
            assert app._compaction_worker is None
            assert not app.query_one(ComposerTextArea).disabled
    finally:
        monkeypatch.setattr(session, "flush", original_flush)
        await app._turn_runner.shutdown()
        session.close()


async def test_final_state_is_shown_even_when_runner_failed_to_save_it(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    provider = GateSummaryProvider()
    app, session = _build_app(provider, openai_provider_config, ui_config, tmp_path)
    _history(session)
    original_flush = session.flush

    def unavailable_storage(*args, **kwargs):
        raise SessionRepositoryError("磁盘暂时不可写")

    try:
        async with app.run_test() as pilot:
            await _submit_message(app, pilot, "/compact")
            await _until(pilot, provider.started.is_set)
            widget = app.query_one(CompactionActivityWidget)
            monkeypatch.setattr(session, "flush", unavailable_storage)
            provider.release.set()
            await _until(pilot, lambda: not app._is_streaming)
            assert widget.activity.status == "failed"
            assert widget._spinner_timer is None
            assert not app.query_one(ComposerTextArea).disabled
            monkeypatch.setattr(session, "flush", original_flush)
    finally:
        monkeypatch.setattr(session, "flush", original_flush)
        await app._turn_runner.shutdown()
        session.close()


async def test_manual_failure_is_readable_in_chat_and_survives_session_resume(
    openai_provider_config, ui_config, tmp_path, monkeypatch,
):
    app, session = _build_app(FakeProvider([RuntimeError("摘要服务断开")]),
                              openai_provider_config, ui_config, tmp_path)
    _history(session)
    session_id = session.session_id
    notifications = []
    monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notifications.append(args))
    try:
        async with app.run_test() as pilot:
            await _submit_message(app, pilot, "/compact")
            await _until(pilot, lambda: not app._is_streaming)
            widget = app.query_one(CompactionActivityWidget)
            assert widget.activity.status == "failed"
            assert "摘要服务断开" in widget.activity.error_text
            assert widget.activity.after_tokens is None
            assert not notifications
            session.close()
            session.new_session()
            session.resume_session(session_id)
            await app._restore_session_view()
            await pilot.pause()
            restored = app.query_one(CompactionActivityWidget)
            assert restored.activity.status == "failed"
            restored.header.focus()
            await pilot.press("space")
            assert not restored.collapsed
            assert "摘要服务断开" in str(restored.body.render())
    finally:
        await app._turn_runner.shutdown()
        session.close()


@pytest.mark.parametrize("width", [32, 60, 100])
async def test_compaction_details_remain_reachable_without_covering_the_composer(
    openai_provider_config, ui_config, tmp_path, width,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    session.create_user_message("请整理上下文")
    activity = session.begin_compaction("manual")
    session.finish_compaction(activity.id, status="completed", result=ContextCompactionResult(48_000, 12_000))
    try:
        async with app.run_test(size=(width, 16)) as pilot:
            await app._restore_session_view()
            await pilot.pause()
            widget = app.query_one(CompactionActivityWidget)
            widget.header.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert not widget.collapsed
            assert "48,000" in str(widget.body.render())
            assert "12,000" in str(widget.body.render())
            assert "75.0%" in str(widget.body.render())
            assert widget.header.region.right <= width
            assert widget.body.region.right <= width
            composer = app.query_one(ComposerTextArea)
            assert composer.region.y >= 0 and composer.region.bottom <= 16
            assert app.query_one("#chat-view", VerticalScroll).region.bottom <= app.query_one("#composer-region").region.y
            await pilot.press("space")
            assert widget.collapsed
    finally:
        await app._turn_runner.shutdown()
        session.close()


async def test_restored_manual_rows_keep_their_message_anchors_and_automatic_row_stays_inline(
    openai_provider_config, ui_config, tmp_path,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    first = session.create_user_message("第一条消息")
    manual = session.begin_compaction("manual")
    session.finish_compaction(manual.id, status="cancelled")
    another = session.begin_compaction("manual")
    session.finish_compaction(another.id, status="failed", error_text="不足以缩小上下文")
    second = session.create_user_message("后续消息")
    assistant = session.create_assistant_message()
    session.append_message_content(assistant.id, "压缩之前的正文")
    automatic = session.begin_compaction("automatic", message_id=assistant.id)
    session.finish_compaction(automatic.id, status="completed", result=ContextCompactionResult(1000, 250))
    session.append_message_content(assistant.id, "压缩之后的正文")
    session.complete_message(assistant.id)
    session_id = session.session_id
    session.close()
    session.new_session()
    session.resume_session(session_id)
    try:
        async with app.run_test() as pilot:
            await app._restore_session_view()
            await pilot.pause()
            children = list(app.query_one("#chat-view", VerticalScroll).children)
            identities = [child.message_id if isinstance(child, MessageWidget) else child.activity_id for child in children]
            assert identities == [first.id, manual.id, another.id, second.id, assistant.id]
            widget = app._message_widgets[assistant.id]
            inline = widget.query_one(CompactionActivityWidget)
            assert inline.activity_id == automatic.id
            assert inline.activity.status == "completed"
            blocks = list(widget.query_one(".message-timeline").children)
            assert blocks.index(inline) > 0
            assert "压缩之前" in _rendered_text(blocks[0])
            assert "压缩之后" in _rendered_text(blocks[-1])
    finally:
        await app._turn_runner.shutdown()
        session.close()


async def test_compaction_events_do_not_pull_a_reader_away_from_history(
    openai_provider_config, ui_config, tmp_path,
):
    app, session = _build_app(FakeProvider([]), openai_provider_config, ui_config, tmp_path)
    for index in range(15):
        session.create_user_message(f"历史消息 {index}\n" * 4)
    try:
        async with app.run_test(size=(64, 20)) as pilot:
            await app._restore_session_view()
            await pilot.pause()
            chat = app.query_one("#chat-view", VerticalScroll)
            chat.scroll_home(animate=False)
            await pilot.pause()
            position = chat.scroll_y
            assert not chat.is_vertical_scroll_end
            activity = session.begin_compaction("manual")
            event = TurnEvent(kind="compaction_updated", compaction=deepcopy(activity))
            await app._consume_turn_event(event)
            await app._consume_turn_event(event)
            await pilot.pause()
            assert len(app._compaction_widgets) == 1
            assert chat.scroll_y == position
            completed = session.finish_compaction(activity.id, status="completed", result=ContextCompactionResult(800, 400))
            await app._consume_turn_event(TurnEvent(kind="compaction_updated", compaction=completed))
            await pilot.pause()
            assert chat.scroll_y == position
    finally:
        await app._turn_runner.shutdown()
        session.close()
