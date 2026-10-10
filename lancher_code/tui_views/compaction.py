"""压缩是有起止状态的会话活动；进度刷新不改变用户的阅读选择。"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual.app import ComposeResult
from textual.timer import Timer
from textual.widgets import Static

from lancher_code.models import CompactionActivity
from lancher_code.tui_views.theme import theme_palette
from lancher_code.tui_views.timeline import TraceSection


class CompactionActivityWidget(TraceSection):
    DEFAULT_CSS = """
    CompactionActivityWidget { height: auto; width: 1fr; margin: 0; }
    CompactionActivityWidget .compaction-trace-header {
        height: auto; width: 1fr; color: $text-muted;
    }
    CompactionActivityWidget .compaction-trace-header:focus { text-style: bold underline; }
    CompactionActivityWidget .compaction-body {
        height: auto; width: 1fr; padding-left: 2; color: $text-muted;
    }
    """

    SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
    STATUS_LABELS = {
        "running": "正在压缩上下文（可能需要一段时间）",
        "completed": "已压缩上下文",
        "failed": "压缩失败",
        "cancelled": "压缩已停止",
        "interrupted": "压缩已中断",
    }
    TRIGGER_LABELS = {"manual": "手动压缩", "automatic": "自动压缩", "emergency": "超限后压缩"}

    def __init__(self, activity: CompactionActivity) -> None:
        super().__init__(kind="compaction", collapsed=True)
        self.activity_id = activity.id
        self.activity = activity
        self.body = Static(classes="trace-body compaction-body", markup=False)
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.body

    def on_mount(self) -> None:
        self._sync_view()
        self._sync_spinner()

    def on_unmount(self) -> None:
        self._stop_spinner()

    def on_resize(self) -> None:
        if self.is_mounted:
            self._sync_view()

    def update_activity(self, activity: CompactionActivity) -> None:
        if activity.id != self.activity_id:
            raise ValueError("压缩控件只能更新同一条活动记录。")
        self.activity = activity
        if self.is_mounted:
            self._sync_view()
            self._sync_spinner()

    def collapse_for_completion(self) -> None:
        # 压缩默认收起；正在阅读详情时，外层轮次完成也不能藏起它。
        self._completion_collapsed = True
        if not self._manual:
            self._collapsed = True
            self._sync_view()

    def _sync_spinner(self) -> None:
        if self.activity.status == "running":
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(0.14, self._advance_spinner)
        else:
            self._stop_spinner()

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _advance_spinner(self) -> None:
        if not self.is_mounted or self.activity.status != "running":
            self._stop_spinner()
            return
        self._spinner_frame = (self._spinner_frame + 1) % len(self.SPINNER_FRAMES)
        self._sync_view()

    def _sync_view(self) -> None:
        colors = theme_palette(self.app.theme)
        status = self.activity.status
        marker = self.SPINNER_FRAMES[self._spinner_frame] if status == "running" else {
            "completed": "✓", "failed": "×", "cancelled": "■", "interrupted": "!",
        }.get(status, "!")
        tone = colors["primary"] if status == "running" else colors["success"] if status == "completed" else colors["error"] if status == "failed" else colors["warning"]
        title = Text("▸ " if self._collapsed else "▾ ", style=colors["muted"], overflow="fold")
        title.append(marker + " ", style=tone)
        label = self.STATUS_LABELS.get(status, "压缩状态未知")
        width = self.header.content_size.width or self.content_size.width
        if status == "running" and 0 < width < Text(title.plain + label).cell_len:
            # 先分开主标题和等待说明，避免 Rich 把图标孤零零留在第一行。
            label = "正在压缩上下文\n    （可能需要一段时间）"
        title.append(label, style=colors["muted"])
        self.header.update(title)
        self.header.styles.height = "auto"
        self.body.display = not self._collapsed
        if self.body.display:
            details = Text("\n".join(self._detail_lines()), style=colors["muted"], overflow="fold")
            if self.activity.error_text:
                details.append("\n原因：" + self.activity.error_text, style=colors["error"])
            if self.activity.continued:
                details.append("\n本轮继续处理。")
            self.body.update(details)

    def _detail_lines(self) -> list[str]:
        activity = self.activity
        if activity.status == "completed":
            before, after = _format_amount(activity.before_tokens), _format_amount(activity.after_tokens)
            lines = [f"上下文估算：{before} → {after} tokens",
                     f"压缩率：{_reduction_text(activity)}（上下文减少比例）"]
        else:
            # 未完成时没有压缩后事实，省略它而不是陈列一组未知结果。
            lines = [] if activity.before_tokens is None else [f"压缩前上下文：{_format_tokens(activity.before_tokens)}"]
        lines.extend([
            f"开始时间：{activity.started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
            f"耗时：{_duration_text(activity)}",
            f"触发方式：{self.TRIGGER_LABELS.get(activity.trigger, '未知')}",
        ])
        sources = {"usage_calibrated": "已校准估算", "estimated": "未校准估算"}
        if activity.before_source in sources:
            lines.append("压缩前计量：" + sources[activity.before_source])
        if activity.after_source in sources:
            lines.append("压缩后计量：" + sources[activity.after_source])
        if activity.dropped_groups:
            lines.append(f"摘要输入省略：{activity.dropped_groups} 组旧历史（原对话保留）")
        lines.extend(["以上为上下文估算。", "摘要请求的已上报用量另计。"])
        return lines


def _format_tokens(tokens: int | None) -> str:
    return "--" if tokens is None else f"≈{tokens:,} token"


def _format_amount(tokens: int | None) -> str:
    return "--" if tokens is None else f"≈{tokens:,}"


def _reduction_text(activity: CompactionActivity) -> str:
    before, after = activity.before_tokens, activity.after_tokens
    if activity.status != "completed" or before is None or after is None or before <= 0:
        return "--"
    return f"{(before - after) / before:.1%}"


def _duration_text(activity: CompactionActivity) -> str:
    # 恢复后的终态必须使用结束时间，不能让旧记录的耗时持续增长。
    end = activity.finished_at
    if end is None:
        if activity.status != "running":
            return "--"
        end = datetime.now(timezone.utc)
    seconds = max(0.0, (end - activity.started_at).total_seconds())
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes} 分 {seconds} 秒"
