from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from lancher_code.context.models import CompactionActivity
from lancher_code.sessions.models import SessionMessage
from lancher_code.mcp.manager import MCPInitializationProgress
from lancher_code.tui.compaction import CompactionActivityWidget
from lancher_code.tui.theme import TerminalMarkdown, theme_palette
from lancher_code.tui.timeline import (
    ThinkingTraceWidget, ToolActivityWidget, timeline_blocks,
)

class BannerWidget(Static):
    def __init__(self, cwd: Path) -> None:
        super().__init__(id="banner")
        self._cwd = cwd
        self._compact = False
        self.mcp_status = "MCP：未配置"
        self.context_usage_status = "上下文 --"


    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.set_class(compact, "-compact")
        self.refresh()

    def update_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        if progress.state in {"complete", "catalog_updated", "reconnected"}:
            if progress.total_servers == 0:
                self.mcp_status = "MCP：未配置"
            elif progress.failed_servers:
                self.mcp_status = (
                    f"MCP：初始化完成 · 成功 {progress.successful_servers}/{progress.total_servers}"
                    f" · {progress.registered_tools} 个工具 · {progress.failed_servers} 个失败"
                )
            else:
                self.mcp_status = (
                    f"MCP：已就绪 · {progress.successful_servers}/{progress.total_servers} Server"
                    f" · {progress.registered_tools} 个工具"
                )
            if progress.warning_count:
                self.mcp_status += f" · {progress.warning_count} 条警告"
                self.mcp_status += " · 详情：~/.lancher/logs/lancher-error.log"
        else:
            current = f" · {progress.current_server}：连接中" if progress.current_server else ""
            self.mcp_status = (
                f"MCP：正在初始化 {progress.completed_servers}/{progress.total_servers}"
                f" · 成功 {progress.successful_servers} · 失败 {progress.failed_servers}"
                f" · 工具 {progress.registered_tools}{current}"
            )
        self.refresh()

    def update_context_usage(self, used_tokens: int | None, context_window: int) -> None:
        if used_tokens is None or context_window <= 0:
            self.context_usage_status = "上下文 --"
        else:
            percentage = math.ceil(max(0, used_tokens) * 100 / context_window)
            self.context_usage_status = f"上下文 {min(100, percentage)}%"
        self.refresh()


    def render(self) -> RenderableType:
        header = self._header()
        if self._compact or self.size.width < 64:
            return header

        colors = theme_palette(self.app.theme)
        greeting = Text("一起把想法做成代码。直接描述任务，或输入 / 查看命令。", style=colors["muted"])
        return Group(header, greeting)

    def _header(self) -> Text:
        colors = theme_palette(self.app.theme)
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append("LanCher Code", style=colors["muted"])
        left.append("  ·  ", style=colors["muted"])
        left.append(self._cwd.name, style=colors["muted"])
        return left


class MessageWidget(Vertical):
    ROLE_LABELS = {
        "system": "提示",
        "user": "你",
        "assistant": "LanCher",
    }
    STATUS_LABELS = {
        "error": "未完成",
        "cancelled": "已停止",
    }

    def __init__(self, message: SessionMessage, *, show_thinking: bool,
                 compaction_activities: Mapping[str, CompactionActivity] | None = None) -> None:
        super().__init__(classes=f"message message--{message.role}")
        self.message_id = message.id
        self._show_thinking = show_thinking
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self.trace_collapsed = message.trace.collapsed
        self._message = message
        self._compaction_activities = compaction_activities if compaction_activities is not None else {}
        self._sync_lock = asyncio.Lock()
        self._blocks: dict[str, Static | ThinkingTraceWidget | ToolActivityWidget | CompactionActivityWidget] = {}
        self._completion_collapsed = False
        self._restored_complete = message.role == "assistant" and message.status == "complete"

    def compose(self) -> ComposeResult:
        yield Static(classes="message-label")
        yield Vertical(classes="message-timeline")
        yield Static(classes="message-body")

    async def on_mount(self) -> None:
        await self._sync_view()
        if self._restored_complete:
            self.collapse_for_completion()

    def update_display_preferences(self, *, show_thinking: bool) -> None:
        """更新显示偏好，在当前应用事件结束后刷新已有时间线。"""
        self._show_thinking = show_thinking
        self.app.call_later(self._sync_view)

    async def update_from_message(self, message: SessionMessage, *,
                                  compaction_activities: Mapping[str, CompactionActivity] | None = None) -> None:
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self._message = message
        if compaction_activities is not None:
            self._compaction_activities = compaction_activities
        await self._sync_view()

    async def _sync_view(self) -> None:
        async with self._sync_lock:
            await self._update_view()

    async def _update_view(self) -> None:
        self.set_class(self.status == "error", "-error")

        label_widget = self.query_one(".message-label", Static)
        label_widget.update(Text(self._label_text(), style="bold " + self._label_color()))
        label_widget.styles.color = self._label_color()
        label_widget.styles.text_style = "bold"

        timeline = self.query_one(".message-timeline", Vertical)
        blocks = timeline_blocks(self._message) if self.role == "assistant" else []
        wanted = {block.key for block in blocks}
        for key in list(self._blocks):
            if key not in wanted:
                await self._blocks.pop(key).remove()
        # 增量更新原有控件；不要在每个 delta 重建并丢掉焦点和展开选择。
        previous_kind: str | None = None
        for block in blocks:
            activity = self._compaction_activities.get(block.entries[0].metadata.get("activity_id", "")) if block.kind == "compaction" else None
            widget = self._blocks.get(block.key)
            if activity is not None and widget is not None and not isinstance(widget, CompactionActivityWidget):
                # 不完整投影可能先显示占位；真实记录到达后在原位置替换。
                old_widget = widget
                widget = CompactionActivityWidget(activity)
                await timeline.mount(widget, before=old_widget)
                await old_widget.remove()
                self._blocks[block.key] = widget
            if widget is None:
                if block.kind == "thinking":
                    widget = ThinkingTraceWidget(block.entries, collapsed=block.entries[0].metadata.get("state") != "streaming")
                elif block.kind == "tool":
                    widget = ToolActivityWidget(block.entries, status=self.status)
                elif activity is not None:
                    widget = CompactionActivityWidget(activity)
                else:
                    widget = Static(classes=f"timeline-text timeline-{block.kind}")
                self._blocks[block.key] = widget
                await timeline.mount(widget)
            if isinstance(widget, ThinkingTraceWidget):
                widget.display = self._show_thinking
                widget.update_entries(block.entries)
            elif isinstance(widget, ToolActivityWidget):
                await widget.update_entries(block.entries, status=self.status)
            elif isinstance(widget, CompactionActivityWidget):
                if activity is not None:
                    widget.update_activity(activity)
            else:
                text = block.entries[0].text
                if block.kind == "compaction":
                    widget.update(Text("压缩记录不可用", style=theme_palette(self.app.theme)["muted"]))
                elif block.kind == "text":
                    widget.update(TerminalMarkdown(text, self.app.theme))
                else:
                    colors = theme_palette(self.app.theme)
                    widget.update(Text(text, style=colors["error"] if self.status == "error" else colors["warning"]))
            # 相邻过程紧凑排列；仅在正文/提示与其他块之间留一行。
            separated = previous_kind is not None and (block.kind in {"text", "notice"} or previous_kind in {"text", "notice"})
            widget.set_class(separated, "timeline-separator")
            if widget.display:
                previous_kind = block.kind

        body_widget = self.query_one(".message-body", Static)
        # 助手正文由时间线呈现；其他消息直接显示正文。
        body_text = "" if self.role == "assistant" and blocks else self._body_text()
        body_widget.display = bool(body_text)
        body_widget.set_class(bool(body_text) and previous_kind is not None, "timeline-separator")
        if body_text:
            body_widget.styles.color = self._body_color()
            body_widget.update(TerminalMarkdown(body_text, self.app.theme) if self.role == "assistant" and self.status != "error" else Text(body_text))

    def collapse_for_completion(self) -> None:
        """任务成功结束只收起一次，用户随后重新查看时不再干预。"""
        if self._completion_collapsed or self.role != "assistant" or self.status != "complete":
            return
        self._completion_collapsed = True
        for widget in self._blocks.values():
            if isinstance(widget, (ThinkingTraceWidget, ToolActivityWidget)):
                widget.collapse_for_completion()

    def _label_text(self) -> str:
        name = self.ROLE_LABELS.get(self.role, self.role.upper())
        if self.status in self.STATUS_LABELS:
            return f"{name} · {self.STATUS_LABELS[self.status]}"
        return name

    def _label_color(self) -> str:
        colors = theme_palette(self.app.theme)
        return colors["error"] if self.status == "error" else colors["primary"] if self.role in {"user", "assistant"} else colors["muted"]

    def _body_text(self) -> str:
        if self.status == "error":
            return self.content or "请求失败。"
        if self.status == "cancelled":
            return self.content or "本轮已取消。"
        if self.content:
            return self.content
        if self.status == "streaming" and not self.trace_entries:
            return "等待模型回复..."
        if self.status == "complete" and not self.trace_entries:
            return "本轮未收到任何回复。"
        return ""

    def _body_color(self) -> str:
        colors = theme_palette(self.app.theme)
        return colors["error"] if self.status == "error" else colors["text"]
