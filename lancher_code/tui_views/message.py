from __future__ import annotations

from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click
from textual.widgets import Static

from lancher_code.models import SessionMessage, TraceEntry
from lancher_code.mcp.manager import MCPInitializationProgress

BANNER_TEXT = r"""
    __                ________                 ______          __
   / /   ____ _____  / ____/ /_  ___  _____   / ____/___  ____/ /__
  / /   / __ `/ __ \/ /   / __ \/ _ \/ ___/  / /   / __ \/ __  / _ \
 / /___/ /_/ / / / / /___/ / / /  __/ /     / /___/ /_/ / /_/ /  __/
/_____/\__,_/_/ /_/\____/_/ /_/\___/_/      \____/\____/\__,_/\___/
"""


class BannerWidget(Static):
    def __init__(self, cwd: Path) -> None:
        super().__init__(id="banner")
        self._cwd = cwd
        self._compact = False
        self._mcp_status = "MCP：未配置"

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.set_class(compact, "-compact")
        self.refresh()

    def update_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        if progress.state == "complete":
            if progress.total_servers == 0:
                self._mcp_status = "MCP：未配置"
            elif progress.failed_servers:
                self._mcp_status = (
                    f"MCP：初始化完成 · 成功 {progress.successful_servers}/{progress.total_servers}"
                    f" · {progress.registered_tools} 个工具 · {progress.failed_servers} 个失败"
                )
            else:
                self._mcp_status = (
                    f"MCP：已就绪 · {progress.successful_servers}/{progress.total_servers} Server"
                    f" · {progress.registered_tools} 个工具"
                )
            if progress.warning_count:
                self._mcp_status += f" · {progress.warning_count} 条警告"
                self._mcp_status += " · 详情：~/.lancher/logs/lancher-error.log"
        else:
            current = f" · {progress.current_server}：连接中" if progress.current_server else ""
            self._mcp_status = (
                f"MCP：正在初始化 {progress.completed_servers}/{progress.total_servers}"
                f" · 成功 {progress.successful_servers} · 失败 {progress.failed_servers}"
                f" · 工具 {progress.registered_tools}{current}"
            )
        self.refresh()

    def render(self) -> RenderableType:
        if self._compact:
            compact_text = Text()
            compact_text.append("LanCher Code", style="bold #73b6ff")
            compact_text.append("  ")
            compact_text.append("工作目录：", style="bold #73b6ff")
            compact_text.append(str(self._cwd), style="default")
            compact_text.append("  ")
            compact_text.append(self._mcp_status, style="#97adc7")
            return compact_text

        title = Text(BANNER_TEXT.strip("\n"), style="bold #73b6ff")
        subtitle = Text()
        subtitle.append("当前工作目录：", style="bold #73b6ff")
        subtitle.append(str(self._cwd), style="default")
        status = Text(self._mcp_status, style="#97adc7")
        return Group(title, subtitle, status)


class ThinkingTraceWidget(Vertical):
    def __init__(self, entries: list[TraceEntry], *, collapsed: bool = True) -> None:
        super().__init__(classes="thinking-trace")
        self._entries = list(entries)
        self._collapsed = collapsed

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def compose(self) -> ComposeResult:
        yield Static(classes="thinking-trace-header")
        yield Static(classes="thinking-trace-body")

    def on_mount(self) -> None:
        self._sync_view()

    @on(Click, ".thinking-trace-header")
    def toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self._sync_view()

    def set_collapsed(self, collapsed: bool) -> None:
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._sync_view()

    def update_entries(self, entries: list[TraceEntry]) -> None:
        self._entries = list(entries)
        self._sync_view()

    def _sync_view(self) -> None:
        header = self.query_one(".thinking-trace-header", Static)
        body = self.query_one(".thinking-trace-body", Static)
        marker = "▶" if self._collapsed else "▼"
        header.update(f"{marker} 思考轨迹 ({len(self._entries)})")
        header.styles.color = "#a8b9cc"
        header.styles.text_style = "bold"
        body.display = bool(self._entries) and not self._collapsed
        if body.display:
            body.update(_format_trace_entries(self._entries))


def _format_trace_entries(entries: list[TraceEntry]) -> Text:
    renderable = Text()
    for entry in entries:
        if entry.kind == "thinking":
            renderable.append(entry.text, style="#a8b9cc")
        elif entry.kind == "tool_call":
            renderable.append(_format_tool_call_entry(entry), style="#73b6ff")
        elif entry.kind == "tool_result":
            prefix = "✓ " if entry.ok else "✗ "
            style = "#78d98a" if entry.ok else "#ff7b72"
            renderable.append(f"{prefix}{entry.text}", style=style)
            for display_line in entry.metadata.get("display_lines", []):
                if not isinstance(display_line, dict):
                    continue
                line_text = display_line.get("text")
                if not isinstance(line_text, str):
                    continue
                tone = display_line.get("tone")
                line_style = "#78d98a" if tone == "success" else "#ff7b72" if tone == "error" else style
                renderable.append("\n")
                renderable.append(line_text, style=line_style)
        elif entry.kind == "text":
            renderable.append(entry.text, style="#e8e8e8")
        elif entry.kind == "notice":
            renderable.append(f"提示：{entry.text}", style="#ffb86c")
        renderable.append("\n")

    if renderable.plain.endswith("\n"):
        renderable.rstrip()
    return renderable


def _format_tool_call_entry(entry: TraceEntry) -> str:
    if not entry.arguments:
        return f"● {entry.tool_name}"
    parts: list[str] = []
    for key, value in entry.arguments.items():
        rendered = str(value)
        if len(rendered) > 24:
            rendered = rendered[:24] + "..."
        parts.append(f"{key}={rendered}")
    return f"● {entry.tool_name}({', '.join(parts[:2])})"


class MessageWidget(Vertical):
    ROLE_LABELS = {
        "system": "SYSTEM",
        "user": "YOU",
        "assistant": "LANCHER",
    }
    ROLE_STYLES = {
        "system": "#97adc7",
        "user": "#78d98a",
        "assistant": "#73b6ff",
    }
    STATUS_LABELS = {
        "error": "ERROR",
        "cancelled": "CANCELLED",
    }

    def __init__(self, message: SessionMessage, *, show_thinking: bool) -> None:
        super().__init__(classes=f"message message--{message.role}")
        self.message_id = message.id
        self._show_thinking = show_thinking
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self.trace_collapsed = message.trace.collapsed

    def compose(self) -> ComposeResult:
        yield Static(classes="message-label")
        yield ThinkingTraceWidget(self.trace_entries, collapsed=self.trace_collapsed)
        yield Static(classes="message-body")

    def on_mount(self) -> None:
        self._sync_view()

    def update_from_message(self, message: SessionMessage) -> None:
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self.trace_collapsed = message.trace.collapsed
        self._sync_view()

    def _sync_view(self) -> None:
        self.set_class(self.status in {"error", "cancelled"}, "-error")

        label_widget = self.query_one(".message-label", Static)
        label_widget.update(self._label_text())
        label_widget.styles.color = self._label_color()
        label_widget.styles.text_style = "bold"

        trace_widget = self.query_one(ThinkingTraceWidget)
        trace_visible = self._show_trace()
        trace_widget.display = trace_visible
        if trace_visible:
            trace_widget.set_collapsed(self.trace_collapsed)
            trace_widget.update_entries(self.trace_entries)

        body_widget = self.query_one(".message-body", Static)
        body_text = self._body_text()
        body_widget.display = bool(body_text)
        if body_text:
            body_widget.styles.color = self._body_color()
            body_widget.update(body_text)

    def _show_trace(self) -> bool:
        return self._show_thinking and self.role == "assistant" and bool(self.trace_entries)

    def _label_text(self) -> str:
        if self.status in self.STATUS_LABELS:
            return self.STATUS_LABELS[self.status]
        return self.ROLE_LABELS.get(self.role, self.role.upper())

    def _label_color(self) -> str:
        if self.status in {"error", "cancelled"}:
            return "#ff7b72"
        return self.ROLE_STYLES.get(self.role, "#ffffff")

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
        if self.status in {"error", "cancelled"}:
            return "#ff7b72"
        return "#e8e8e8"
