"""按发生顺序保留过程；分组与单次调用各自管理折叠状态。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click
from textual.widgets import Static

from lancher_code.models import SessionMessage, TraceEntry
from lancher_code.tui_views.theme import theme_palette


class TraceHeader(Static):
    can_focus = True
    BINDINGS = [("enter", "toggle", "展开/收起"), ("space", "toggle", "展开/收起")]

    def __init__(self, toggle: Callable[[], None], *, kind: str) -> None:
        super().__init__(classes=f"trace-header {kind}-trace-header")
        self._toggle = toggle

    def on_click(self, event: Click) -> None:
        # 点击内层调用不能冒泡到整组，也不能移走输入区的草稿。
        event.stop()
        self.focus()
        self.action_toggle()

    def action_toggle(self) -> None:
        self._toggle()


class TraceSection(Vertical):
    def __init__(self, *, kind: str, collapsed: bool = True) -> None:
        super().__init__(classes=f"trace-section {kind}-trace")
        self._collapsed = collapsed
        self._manual = False
        self._completion_collapsed = False
        self.header = TraceHeader(self.toggle_collapsed, kind=kind)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._manual = True
        self._collapsed = collapsed
        self._sync_view()

    def collapse_for_completion(self) -> None:
        """任务完成时统一收起一次，之后仍允许用户展开阅读。"""
        if self._completion_collapsed:
            return
        self._completion_collapsed = True
        self.set_collapsed(True)

    def _default_collapsed(self, collapsed: bool) -> None:
        if not self._manual:
            self._collapsed = collapsed

    def _sync_view(self) -> None:
        raise NotImplementedError


class ThinkingTraceWidget(TraceSection):
    def __init__(self, entries: list[TraceEntry], *, collapsed: bool = True) -> None:
        super().__init__(kind="thinking", collapsed=collapsed)
        self._entries = entries
        self.body = Static(classes="trace-body thinking-trace-body")

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.body

    def on_mount(self) -> None:
        self._sync_view()

    def update_entries(self, entries: list[TraceEntry]) -> None:
        self._entries = entries
        self._default_collapsed(not any(entry.metadata.get("state") == "streaming" for entry in entries))
        self._sync_view()

    def _sync_view(self) -> None:
        first, remaining = _thinking_display_parts(self._entries)
        label = f"▸ {first}…" if self._collapsed else f"▾ {first}"
        self.header.display = bool(first)
        self.header.styles.height = 1 if self._collapsed else "auto"
        self.header.update(Text(label, style=theme_palette(self.app.theme)["muted"],
                                no_wrap=self._collapsed, overflow="ellipsis"))
        self.body.display = bool(remaining) and not self._collapsed
        self.body.update(Text(remaining, style=theme_palette(self.app.theme)["muted"]))


def _thinking_display_parts(entries: list[TraceEntry]) -> tuple[str, str]:
    """摘要使用首个可读行，仅在展示层清理边界空白，保留原始记录。"""
    text = "\n".join(entry.text for entry in entries)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return "", ""
    first = " ".join(lines[first_index].split())
    body_lines = lines[first_index + 1:]
    # Header 已占一行；段首换行不应再制造一条看似空白的思考。
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    return first, "\n".join(body_lines)


TOOL_LABELS = {
    "read_file": ("▤", "读取文件"), "find_files": ("⌕", "查找文件"),
    "search_code": ("⌕", "搜索代码"), "tool_search": ("⌕", "查找工具"),
    "write_file": ("✎", "写入文件"), "replace_in_file": ("✎", "修改文件"),
    "write_plan_file": ("✎", "保存计划"), "bash": ("›_", "运行命令"),
    "run_command": ("›_", "运行命令"),
    "glob": ("⌕", "查找文件"), "grep": ("⌕", "搜索代码"), "edit_file": ("✎", "修改文件"),
}
STATE_LABELS = {
    "queued": "等待执行", "running": "执行中", "awaiting_permission": "待批准",
    "complete": "✓ 完成", "error": "× 失败", "cancelled": "已停止", "skipped": "已跳过",
    "not_executed": "未执行",
}
ACTIVE_STATES = {"queued", "running", "awaiting_permission"}
ISSUE_STATES = {"error", "cancelled", "skipped", "awaiting_permission", "not_executed"}


def call_state(call: TraceEntry, result: TraceEntry | None, status: str) -> str:
    if result is not None:
        state = result.metadata.get("state")
        if result.ok is False and result.metadata.get("started") is False and state == "error":
            return "not_executed"
        if state in STATE_LABELS:
            return str(state)
        return "complete" if result.ok else "error"
    state = call.metadata.get("state")
    if status != "streaming" and state in ACTIVE_STATES | {None}:
        return "cancelled"
    return str(state) if state in STATE_LABELS else "queued"


class ToolCallWidget(TraceSection):
    def __init__(self, call: TraceEntry, result: TraceEntry | None, *, status: str = "complete",
                 on_toggle: Callable[[], None] | None = None) -> None:
        super().__init__(kind="tool-call")
        self.call_id = call.call_id
        self.call, self.result, self.status = call, result, status
        self._on_toggle = on_toggle
        self.body = Static(classes="trace-body tool-call-body", markup=False)

    @property
    def state(self) -> str:
        return call_state(self.call, self.result, self.status)

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.body

    def set_collapsed(self, collapsed: bool) -> None:
        super().set_collapsed(collapsed)
        if self._on_toggle is not None:
            self._on_toggle()

    def on_mount(self) -> None:
        self.update_call(self.call, self.result, status=self.status)

    def update_call(self, call: TraceEntry, result: TraceEntry | None, *, status: str) -> None:
        self.call, self.result, self.status = call, result, status
        self._default_collapsed(self.state not in ISSUE_STATES)
        self._sync_view()

    def _sync_view(self) -> None:
        colors = theme_palette(self.app.theme)
        state = self.state
        icon, name = TOOL_LABELS.get(self.call.tool_name, ("◇", self.call.tool_name or "调用"))
        short_name = Text(name)
        short_name.truncate(12, overflow="ellipsis")
        target = next((self.call.arguments[key] for key in ("path", "file_path", "command", "pattern", "query")
                       if self.call.arguments.get(key)), "")
        # 状态放在目标之前，窄终端裁去长路径时仍可识别当前状态。
        label = f"{'▸' if self._collapsed else '▾'} {icon} {short_name.plain} · {STATE_LABELS[state]}"
        if target:
            label += f" · {str(target).splitlines()[0]}"
        color = colors["error"] if state == "error" else colors["warning"] if state in ISSUE_STATES else colors["muted"]
        self.header.update(Text(label, style=color, no_wrap=True, overflow="ellipsis"))
        self.body.display = not self._collapsed
        if self.body.display:
            details = Text(style=colors["muted"], overflow="fold")
            details.append(self.call.tool_name or name)
            if self.call.arguments:
                details.append("\n" + json.dumps(self.call.arguments, ensure_ascii=False, indent=2, default=str))
            if self.result is not None:
                if self.result.text:
                    details.append("\n" + self.result.text)
                content = self.result.metadata.get("content")
                if isinstance(content, str) and content and content != self.result.text:
                    details.append("\n" + content)
                for line in self.result.metadata.get("display_lines", []):
                    if isinstance(line, dict) and isinstance(line.get("text"), str):
                        tone = line.get("tone")
                        color = colors["success"] if tone == "success" else colors["error"] if tone == "error" else colors["muted"]
                        details.append("\n" + line["text"], style=color)
            self.body.update(details)


def _calls_with_results(entries: list[TraceEntry]) -> list[tuple[TraceEntry, TraceEntry | None]]:
    calls = [entry for entry in entries if entry.kind == "tool_call"]
    results = [entry for entry in entries if entry.kind == "tool_result"]
    paired = []
    for call in calls:
        result = next((entry for entry in results if entry.call_id == call.call_id
                       and (call.call_id or entry.tool_name == call.tool_name)), None)
        if result is not None:
            results.remove(result)
        paired.append((call, result))
    for result in results:
        # 旧记录可能只有结果，保留它而不伪造一次正在运行的调用。
        paired.append((TraceEntry(kind="tool_call", call_id=result.call_id, tool_name=result.tool_name), result))
    return paired


class ToolActivityWidget(TraceSection):
    def __init__(self, entries: list[TraceEntry], *, status: str = "complete") -> None:
        super().__init__(kind="tool")
        self._entries = entries
        self.status = status
        self.body = Vertical(classes="tool-calls")
        self._calls: dict[str, ToolCallWidget] = {}

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.body

    async def on_mount(self) -> None:
        await self.update_entries(self._entries, status=self.status)

    def collapse_for_completion(self) -> None:
        if self._completion_collapsed:
            return
        # 单次调用没有父标题，也必须同步折叠自己的详情。
        for widget in self._calls.values():
            widget.collapse_for_completion()
        super().collapse_for_completion()

    async def update_entries(self, entries: list[TraceEntry], *, status: str = "complete") -> None:
        self._entries, self.status = entries, status
        for index, (call, result) in enumerate(_calls_with_results(entries)):
            key = call.call_id or f"legacy-{index}"
            widget = self._calls.get(key)
            if widget is None:
                widget = ToolCallWidget(call, result, status=status, on_toggle=self._refresh_collapse)
                self._calls[key] = widget
                await self.body.mount(widget)
                if self._completion_collapsed:
                    widget.collapse_for_completion()
            else:
                widget.update_call(call, result, status=status)
        self._refresh_collapse()

    def _refresh_collapse(self) -> None:
        states = [widget.state for widget in self._calls.values()]
        # 正在阅读单条详情时，整组完成也不能把它自动藏起来。
        reading_details = any(widget._manual and not widget.collapsed for widget in self._calls.values())
        self._default_collapsed(not reading_details and not any(state in ACTIVE_STATES | ISSUE_STATES for state in states))
        self._sync_view()

    def _sync_view(self) -> None:
        states = [widget.state for widget in self._calls.values()]
        single = len(states) == 1
        # 单次调用直接使用自身标题，不套两个相同的折叠入口。
        self.header.display = not single
        self.body.display = single or not self._collapsed
        self.body.set_class(single, "-single")
        if single:
            return
        colors = theme_palette(self.app.theme)
        active = [state for state in states if state in ACTIVE_STATES]
        labels = []
        if active:
            for state in ("running", "awaiting_permission", "queued", "complete"):
                count = states.count(state)
                if count:
                    labels.append(f"{count} 项{STATE_LABELS[state].removeprefix('✓ ')}")
        else:
            executed = sum(
                widget.result is not None
                and (widget.result.metadata.get("started") is True
                     or ("started" not in widget.result.metadata and widget.state in {"complete", "error"}))
                for widget in self._calls.values()
            )
            labels.append(f"已执行 {executed} 个工具")
        for state in ("error", "not_executed", "cancelled", "skipped"):
            count = states.count(state)
            if count:
                labels.append(f"{count} 项{STATE_LABELS[state].removeprefix('× ')}")
        color = colors["error"] if "error" in states else colors["warning"] if any(state in ISSUE_STATES for state in states) else colors["muted"]
        self.header.update(Text(f"{'▸' if self._collapsed else '▾'} " + " · ".join(labels), style=color,
                                no_wrap=True, overflow="ellipsis"))


@dataclass
class TimelineBlock:
    key: str
    kind: str
    entries: list[TraceEntry] = field(default_factory=list)


def timeline_blocks(message: SessionMessage) -> list[TimelineBlock]:
    """工具结果归回调用位置，正文与思考严格沿用保存的段顺序。"""
    blocks: list[TimelineBlock] = []
    groups: dict[str, TimelineBlock] = {}
    call_groups: dict[str, TimelineBlock] = {}
    legacy_group: TimelineBlock | None = None
    for index, entry in enumerate(message.trace.entries):
        if entry.kind == "thinking" and not entry.text.strip():
            # 流式响应可先发来换行；保留记录，但不挂载空白折叠入口。
            legacy_group = None
            continue
        if entry.kind == "tool_call":
            group_id = entry.metadata.get("group_id")
            key = f"group-{group_id}" if group_id else legacy_group.key if legacy_group else f"legacy-group-{index}"
            if key not in groups:
                groups[key] = TimelineBlock(key, "tool")
                blocks.append(groups[key])
            group = groups[key]
            group.entries.append(entry)
            call_groups[entry.call_id] = group
            legacy_group = group
        elif entry.kind == "tool_result":
            group = call_groups.get(entry.call_id)
            if group is None:
                group = TimelineBlock(f"result-{index}", "tool")
                blocks.append(group)
            group.entries.append(entry)
            legacy_group = None
        else:
            blocks.append(TimelineBlock(f"entry-{index}", entry.kind, [entry]))
            legacy_group = None
    return blocks
