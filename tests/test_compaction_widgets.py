"""压缩活动必须保留时间线顺序、折叠选择，并在窄终端真实可读。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from xml.etree import ElementTree

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from lancher_code.models import CompactionActivity, SessionMessage, ThinkingTrace, TraceEntry
from lancher_code.tui_views.compaction import CompactionActivityWidget
from lancher_code.tui_views.message import MessageWidget
from lancher_code.tui_views.timeline import timeline_blocks


class ActivityApp(App):
    CSS = """
    #activity-scroll { height: 1fr; }
    #draft { height: 3; }
    MessageWidget, .message-timeline, .message-body, .message-label { height: auto; }
    .trace-header { height: 1; }
    """

    def __init__(self, widget) -> None:
        super().__init__()
        self.activity_widget = widget

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="activity-scroll"):
            yield self.activity_widget
        yield Input("未发送的草稿", id="draft")


def _activity(**fields) -> CompactionActivity:
    started_at = datetime.now(timezone.utc) - timedelta(seconds=12)
    return CompactionActivity(id="compaction-one", trigger="manual", status="running", started_at=started_at,
                              before_tokens=82_400, before_source="usage_calibrated", **fields)


def _message() -> SessionMessage:
    return SessionMessage(id="assistant-one", role="assistant", content="", status="streaming",
                          timestamp=datetime.now(timezone.utc), trace=ThinkingTrace(), timeline_version=1)


def _plain(widget: Static) -> str:
    return widget.content.plain


def _renderable_text(widget: Static) -> str:
    output = StringIO()
    Console(file=output, width=120, color_system=None).print(widget.content)
    return output.getvalue()


def _rendered_text(app) -> str:
    root = ElementTree.fromstring(app.export_screenshot())
    return "\n".join("".join(element.itertext()) for element in root.iter()
                     if element.tag.rsplit("}", 1)[-1] == "text").replace("\u00a0", " ")


def _rendered_rows(app) -> list[str]:
    root = ElementTree.fromstring(app.export_screenshot())
    rows = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "text":
            rows.setdefault(element.get("y"), []).append("".join(element.itertext()))
    return ["".join(parts).replace("\u00a0", " ") for parts in rows.values()]


async def test_running_activity_keeps_manual_fold_and_stops_spinner_on_completion():
    activity = _activity()
    widget = CompactionActivityWidget(activity)
    app = ActivityApp(widget)
    async with app.run_test() as pilot:
        assert widget.collapsed and not widget.body.display
        assert widget._spinner_timer is not None
        first_frame = widget._spinner_frame
        await pilot.pause(0.2)
        assert widget._spinner_frame != first_frame
        await pilot.click(widget.header)
        assert not widget.collapsed and widget.body.display
        assert "压缩前上下文：≈82,400 token" in _plain(widget.body)
        assert "开始时间：" in _plain(widget.body)
        assert "压缩后" not in _plain(widget.body)
        assert "压缩率" not in _plain(widget.body)

        completed = replace(activity, status="completed", finished_at=activity.started_at + timedelta(seconds=72),
                            after_tokens=21_300, after_source="estimated", dropped_groups=2)
        widget.update_activity(completed)
        widget.collapse_for_completion()
        assert not widget.collapsed
        assert "已压缩上下文" in _plain(widget.header)
        assert "上下文估算：≈82,400 → ≈21,300 tokens" in _plain(widget.body)
        assert "压缩率：74.2%（上下文减少比例）" in _plain(widget.body)
        assert "耗时：1 分 12 秒" in _plain(widget.body)
        assert "摘要输入省略：2 组旧历史（原对话保留）" in _plain(widget.body)
        assert widget._spinner_timer is None
        finished_frame = widget._spinner_frame
        await pilot.pause(0.35)
        assert widget._spinner_frame == finished_frame
        widget.header.focus()
        await pilot.press("space")
        assert widget.collapsed
        await pilot.press("enter")
        assert not widget.collapsed
        await widget.remove()
        assert widget._spinner_timer is None


async def test_unmounting_running_activity_stops_its_timer():
    widget = CompactionActivityWidget(_activity())
    app = ActivityApp(widget)
    async with app.run_test() as pilot:
        assert widget._spinner_timer is not None
        await widget.remove()
        assert widget._spinner_timer is None
        frame = widget._spinner_frame
        await pilot.pause(0.35)
        assert widget._spinner_frame == frame


@pytest.mark.parametrize("status,label", [("failed", "压缩失败"), ("cancelled", "压缩已停止"),
                                         ("interrupted", "压缩已中断")])
async def test_terminal_activity_shows_its_real_outcome_without_a_spinner(status, label):
    activity = replace(_activity(), status=status, error_text="摘要未通过验证", continued=status == "failed")
    widget = CompactionActivityWidget(activity)
    app = ActivityApp(widget)
    async with app.run_test() as pilot:
        assert label in _plain(widget.header)
        assert widget._spinner_timer is None
        await pilot.click(widget.header)
        assert "原因：摘要未通过验证" in _plain(widget.body)
        assert "耗时：--" in _plain(widget.body)
        assert "压缩率" not in _plain(widget.body)
        assert "压缩后" not in _plain(widget.body)
        assert ("本轮继续处理。" in _plain(widget.body)) == activity.continued


@pytest.mark.parametrize("size", [(32, 16), (80, 24), (120, 40)])
async def test_activity_wraps_and_scrolls_without_hiding_the_draft(size, tmp_path):
    activity = _activity()
    widget = CompactionActivityWidget(activity)
    app = ActivityApp(widget)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        text = _rendered_text(app)
        assert "正在压缩上下文" in text
        assert "可能需要一段时间" in text
        if size[0] == 32:
            assert widget.header.region.height > 1
            assert any("▸" in row and "正在压缩上下文" in row
                       and any(marker in row for marker in widget.SPINNER_FRAMES)
                       for row in _rendered_rows(app))
        await pilot.click(widget.header)
        widget.update_activity(replace(activity, status="completed", finished_at=datetime.now(timezone.utc),
                                       after_tokens=21_300, after_source="estimated"))
        await pilot.pause()
        viewport = app.query_one("#activity-scroll", VerticalScroll)
        seen = set()
        for _ in range(20):
            text = _rendered_text(app)
            seen.update(label for label in ("上下文估算", "82,400", "21,300", "压缩率", "已上报用量另计") if label in text)
            if len(seen) == 5:
                break
            viewport.scroll_down(animate=False)
            await pilot.pause(0.05)
        assert seen == {"上下文估算", "82,400", "21,300", "压缩率", "已上报用量另计"}
        draft = app.query_one("#draft", Input)
        assert not draft.disabled
        assert 0 <= draft.region.y < draft.region.bottom <= size[1]
        draft.focus()
        await pilot.press("end", "x")
        assert draft.value == "未发送的草稿x"
        (tmp_path / f"compaction-{size[0]}x{size[1]}.svg").write_text(app.export_screenshot(), encoding="utf-8")


async def test_running_title_reflows_on_resize_without_restarting_spinner():
    widget = CompactionActivityWidget(_activity())
    app = ActivityApp(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        timer = widget._spinner_timer
        assert "\n" not in _plain(widget.header)
        await pilot.resize_terminal(32, 16)
        await pilot.pause()
        assert "正在压缩上下文\n    （可能需要一段时间）" in _plain(widget.header)
        assert widget._spinner_timer is timer
        assert any("▸" in row and "正在压缩上下文" in row
                   and any(marker in row for marker in widget.SPINNER_FRAMES)
                   for row in _rendered_rows(app))
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert "\n" not in _plain(widget.header)
        assert widget._spinner_timer is timer


async def test_assistant_compaction_stays_between_old_and_new_output_and_preserves_identity():
    activity = replace(_activity(), trigger="automatic", message_id="assistant-one")
    message = _message()
    message.trace.entries = [TraceEntry(kind="text", text="压缩前的回答"),
                             TraceEntry(kind="compaction", metadata={"activity_id": activity.id})]
    activities = {activity.id: activity}
    widget = MessageWidget(message, show_thinking=True, compaction_activities=activities)
    app = ActivityApp(widget)
    async with app.run_test() as pilot:
        activity_widget = widget.query_one(CompactionActivityWidget)
        await pilot.click(activity_widget.header)
        message.trace.entries.append(TraceEntry(kind="text", text="压缩后的回答"))
        activities[activity.id] = replace(activity, status="completed", finished_at=datetime.now(timezone.utc),
                                          after_tokens=21_300, after_source="estimated")
        await widget.update_from_message(message, compaction_activities=activities)
        message.status = "complete"
        widget.collapse_for_completion()
        await pilot.pause()
        blocks = list(widget.query_one(".message-timeline").children)
        assert len(blocks) == 3
        assert blocks[1] is activity_widget
        assert not activity_widget.collapsed
        assert "压缩前的回答" in _renderable_text(blocks[0])
        assert "压缩后的回答" in _renderable_text(blocks[2])
        assert not widget.query_one(".message-body").display
        assert activity_widget._spinner_timer is None
        assert timeline_blocks(message)[1].key == f"compaction-{activity.id}"


async def test_missing_projection_can_be_replaced_at_its_original_timeline_position():
    activity = _activity()
    message = _message()
    message.trace.entries = [TraceEntry(kind="compaction", metadata={"activity_id": activity.id}),
                             TraceEntry(kind="text", text="后续回答")]
    widget = MessageWidget(message, show_thinking=True)
    app = ActivityApp(widget)
    async with app.run_test():
        timeline = widget.query_one(".message-timeline")
        assert "压缩记录不可用" in str(timeline.children[0].render())
        await widget.update_from_message(message, compaction_activities={activity.id: activity})
        assert isinstance(timeline.children[0], CompactionActivityWidget)
        assert "后续回答" in _renderable_text(timeline.children[1])


@pytest.mark.parametrize("before,after", [(None, None), (0, 0), (0, None)])
async def test_unknown_counts_and_zero_denominator_are_not_presented_as_a_reduction(before, after):
    activity = replace(_activity(), status="completed", before_tokens=before, after_tokens=after,
                       finished_at=datetime.now(timezone.utc))
    widget = CompactionActivityWidget(activity)
    app = ActivityApp(widget)
    async with app.run_test() as pilot:
        await pilot.click(widget.header)
        assert "压缩率：--（上下文减少比例）" in _plain(widget.body)
        assert ("上下文估算：--" in _plain(widget.body)) == (before is None)
        assert ("上下文估算：≈0" in _plain(widget.body)) == (before == 0)
