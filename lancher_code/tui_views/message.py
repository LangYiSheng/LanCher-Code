from __future__ import annotations

import math
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click
from textual.widgets import Static

from lancher_code.models import SessionMessage, TraceEntry
from lancher_code.mcp.manager import MCPInitializationProgress, MCPServerInitialization
from lancher_code.tui_views.theme import TerminalMarkdown, theme_palette

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
        self._mcp_compact_status = "MCP 0/0"
        self._mcp_has_issues = False
        self._mcp_progress: MCPInitializationProgress | None = None
        self._context_usage_status = "上下文 --"
        self._spinner_frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.14, self._advance_spinner)

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        self._compact = compact
        self.set_class(compact, "-compact")
        self.refresh()

    def update_mcp_progress(self, progress: MCPInitializationProgress) -> None:
        self._mcp_progress = progress
        self._mcp_has_issues = bool(progress.failed_servers or progress.warning_count)
        self._mcp_compact_status = (
            f"MCP {progress.successful_servers}/{progress.total_servers}"
        )
        if self._mcp_has_issues:
            self._mcp_compact_status += " !"
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

    def update_context_usage(self, used_tokens: int | None, context_window: int) -> None:
        if used_tokens is None or context_window <= 0:
            self._context_usage_status = "上下文 --"
        else:
            percentage = math.ceil(max(0, used_tokens) * 100 / context_window)
            self._context_usage_status = f"上下文 {min(100, percentage)}%"
        self.refresh()

    def _advance_spinner(self) -> None:
        progress = self._mcp_progress
        if progress is None or progress.state == "complete":
            return
        if any(server.state in {"connecting", "registering"} for server in progress.servers):
            self._spinner_frame = (self._spinner_frame + 1) % 4
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

    def _render_mcp_panel(self) -> RenderableType:
        colors = theme_palette(self.app.theme)
        progress = self._mcp_progress
        if progress is None or not progress.servers:
            return Text(self._mcp_status, style=colors["muted"])

        lines: list[Text] = []
        heading = Text("MCP 服务", style="bold " + colors["text"])
        lines.append(heading)
        for server in progress.servers:
            lines.append(self._render_server_row(server))

        footer = Text()
        footer.append(
            f"  {progress.successful_servers}/{progress.total_servers} 已就绪",
            style=colors["success"] if progress.successful_servers else colors["muted"],
        )
        footer.append(f" · {progress.registered_tools} 个工具", style=colors["muted"])
        if progress.failed_servers:
            footer.append(f" · {progress.failed_servers} 个失败", style=colors["error"])
        if progress.warning_count:
            footer.append(f" · {progress.warning_count} 条警告", style=colors["warning"])
        lines.append(footer)
        return Group(*lines)

    def _render_server_row(self, server: MCPServerInitialization) -> Text:
        colors = theme_palette(self.app.theme)
        spinners = ("◐", "◓", "◑", "◒")
        if server.state == "waiting":
            marker, state_text, style = "○", "等待启动", colors["muted"]
        elif server.state == "connecting":
            marker, state_text, style = spinners[self._spinner_frame], "正在连接…", colors["primary"]
        elif server.state == "registering":
            marker, state_text, style = spinners[self._spinner_frame], "正在注册工具…", colors["primary"]
        elif server.state == "failed":
            marker, state_text, style = "✕", "启动失败", colors["error"]
        elif server.warning_count:
            marker, state_text, style = "!", f"已就绪 · {server.registered_tools} 个工具", colors["warning"]
        else:
            marker, state_text, style = "✓", f"已就绪 · {server.registered_tools} 个工具", colors["success"]

        row = Text("  ")
        row.append(marker, style=f"bold {style}")
        row.append(f" {server.name:<18}", style=colors["text"])
        row.append(state_text, style=style)
        return row


class TraceSection(Vertical):
    can_focus = True
    BINDINGS = [("enter", "toggle_details", "展开/收起"), ("space", "toggle_details", "展开/收起")]

    def __init__(self, entries: list[TraceEntry], *, collapsed: bool = True, kind: str = "thinking") -> None:
        super().__init__(classes=f"trace-section {kind}-trace")
        self._entries = list(entries)
        self._collapsed = collapsed
        self._kind = kind

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def compose(self) -> ComposeResult:
        yield Static(classes=f"trace-header {self._kind}-trace-header")
        yield Static(classes=f"trace-body {self._kind}-trace-body")

    def on_mount(self) -> None:
        self._sync_view()

    @on(Click, ".trace-header")
    def toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self._sync_view()

    def action_toggle_details(self) -> None:
        self.toggle_collapsed()

    def set_collapsed(self, collapsed: bool) -> None:
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._sync_view()

    def update_entries(self, entries: list[TraceEntry]) -> None:
        self._entries = list(entries)
        self._sync_view()

    def _sync_view(self) -> None:
        header = self.query_one(".trace-header", Static)
        body = self.query_one(".trace-body", Static)
        marker = "▶" if self._collapsed else "▼"
        colors = theme_palette(self.app.theme)
        if self._kind == "thinking":
            summary = f"思考 ({len(self._entries)})"
        else:
            calls = sum(entry.kind == "tool_call" for entry in self._entries)
            results = [entry for entry in self._entries if entry.kind == "tool_result"]
            errors = sum(entry.ok is False for entry in results)
            summary = f"工具 {len(results)}/{calls}" if calls else "工作记录"
            if errors:
                summary += f" · {errors} 项未完成"
        failed = self._kind == "tool" and any(entry.kind == "tool_result" and entry.ok is False for entry in self._entries)
        header.update(Text(f"{marker} {summary}", style=colors["error"] if failed else colors["muted"]))
        body.display = bool(self._entries) and not self._collapsed
        if body.display:
            body.update(_format_trace_entries(self._entries, colors=colors))


class ThinkingTraceWidget(TraceSection):
    def __init__(self, entries: list[TraceEntry], *, collapsed: bool = True) -> None:
        super().__init__(entries, collapsed=collapsed)


class ToolActivityWidget(TraceSection):
    def __init__(self, entries: list[TraceEntry]) -> None:
        super().__init__(entries, collapsed=True, kind="tool")


def _format_trace_entries(entries: list[TraceEntry], *, colors: dict[str, str] | None = None) -> Text:
    colors = colors or theme_palette()
    renderable = Text()
    for entry in entries:
        if entry.kind == "thinking":
            renderable.append(entry.text, style=colors["muted"])
        elif entry.kind == "tool_call":
            renderable.append(_format_tool_call_entry(entry), style=colors["primary"])
        elif entry.kind == "tool_result":
            prefix = "✓ " if entry.ok else "✗ "
            style = colors["success"] if entry.ok else colors["error"]
            renderable.append(f"{prefix}{entry.text}", style=style)
            for display_line in entry.metadata.get("display_lines", []):
                if not isinstance(display_line, dict):
                    continue
                line_text = display_line.get("text")
                if not isinstance(line_text, str):
                    continue
                tone = display_line.get("tone")
                line_style = colors["success"] if tone == "success" else colors["error"] if tone == "error" else style
                renderable.append("\n")
                renderable.append(line_text, style=line_style)
        elif entry.kind == "text":
            renderable.append(entry.text, style=colors["text"])
        elif entry.kind == "notice":
            renderable.append(f"提示：{entry.text}", style=colors["warning"])
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
        "system": "提示",
        "user": "你",
        "assistant": "LanCher",
    }
    STATUS_LABELS = {
        "error": "未完成",
        "cancelled": "已停止",
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
        yield Static(classes="message-body")
        yield ToolActivityWidget([])
        yield ThinkingTraceWidget([], collapsed=True)

    def on_mount(self) -> None:
        self._sync_view()

    def update_from_message(self, message: SessionMessage) -> None:
        self.role = message.role
        self.content = message.content
        self.status = message.status
        self.trace_entries = list(message.trace.entries)
        self._sync_view()

    def _sync_view(self) -> None:
        self.set_class(self.status == "error", "-error")

        label_widget = self.query_one(".message-label", Static)
        label_widget.update(self._label_text())
        label_widget.styles.color = self._label_color()
        label_widget.styles.text_style = "bold" if self.status == "error" else "none"

        trace_widget = self.query_one(ThinkingTraceWidget)
        thinking = [entry for entry in self.trace_entries if entry.kind == "thinking"]
        tools = [entry for entry in self.trace_entries if entry.kind != "thinking"]
        activity = self.query_one(ToolActivityWidget)
        activity.display = self.role == "assistant" and bool(tools)
        if activity.display:
            activity.update_entries(tools)
        trace_visible = self._show_trace() and bool(thinking)
        trace_widget.display = trace_visible
        if trace_visible:
            trace_widget.update_entries(thinking)

        body_widget = self.query_one(".message-body", Static)
        body_text = self._body_text()
        body_widget.display = bool(body_text)
        if body_text:
            body_widget.styles.color = self._body_color()
            body_widget.update(TerminalMarkdown(body_text, self.app.theme) if self.role == "assistant" and self.status != "error" else Text(body_text))

    def _show_trace(self) -> bool:
        return self._show_thinking and self.role == "assistant" and bool(self.trace_entries)

    def _label_text(self) -> str:
        if self.status in self.STATUS_LABELS:
            return self.STATUS_LABELS[self.status]
        return self.ROLE_LABELS.get(self.role, self.role.upper())

    def _label_color(self) -> str:
        colors = theme_palette(self.app.theme)
        return colors["error"] if self.status == "error" else colors["muted"]

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
