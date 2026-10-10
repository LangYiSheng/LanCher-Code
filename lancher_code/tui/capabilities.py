"""展示核心提供的能力快照；技能和连接的变更只交给核心入口。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from lancher_code.errors import LanCherError
from lancher_code.tui.chat_controls import ReadOnlyDetailsScreen

Entry = dict[str, object]
Operation = Callable[[str, str | None], str | Awaitable[str]]
MCP_STATE_LABELS = {"waiting": "等待", "connecting": "连接中", "ready": "已连接", "failed": "失败",
                    "refreshing": "刷新中", "disconnected": "已断开", "stopped": "已停止", "closed": "已关闭"}


def mcp_details(entry: Entry) -> str:
    state = str(entry.get("state", "未知"))
    lines = [f'MCP · {entry["name"]}', f'状态：{MCP_STATE_LABELS.get(state, state)}',
             f'工具数量：{entry.get("registered_tools", 0)}']
    for key, label in (("transport", "连接类型"), ("scope", "来源"),
                       ("last_error", "最近错误"), ("error", "错误"), ("warning_count", "提示数量"),
                       ("capabilities", "服务器能力"), ("warnings", "提示")):
        if entry.get(key):
            lines.append(f"{label}：{entry[key]}")
    return "\n\n".join(lines)


class CapabilitiesScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "返回")]
    CSS = """
    CapabilitiesScreen { align: center middle; background: $background 70%; }
    #capability-box { width: 94%; max-width: 96; height: 90%; background: $surface; padding: 1; }
    #capability-title { height: 1; text-style: bold; }
    #capability-table { height: 1fr; min-height: 3; margin-top: 1; }
    #capability-summary { height: auto; max-height: 3; color: $text-muted; }
    #capability-notice { height: auto; max-height: 2; }
    .capability-actions { height: 1; margin-top: 1; }
    .capability-actions Button { width: 1fr; min-width: 4; height: 1; border: none; padding: 0; background: transparent; color: $text; text-style: none; }
    .capability-actions Button:focus { background: $foreground; color: $background; text-style: bold; }
    """

    def __init__(self, kind: str, entries: Callable[[], list[Entry]],
                 show_detail: Callable[[str], str], operation: Operation) -> None:
        super().__init__()
        self.kind = kind
        self._entries = entries
        self._show_detail = show_detail
        self._operation = operation
        self._rows: dict[str, Entry] = {}
        self._working = False

    def compose(self) -> ComposeResult:
        with Vertical(id="capability-box"):
            yield Static("Skills · 技能" if self.kind == "skills" else "MCP · 服务器", id="capability-title")
            yield DataTable(cursor_type="row", show_row_labels=False, id="capability-table")
            yield Static("", id="capability-summary", markup=False)
            yield Static("", id="capability-notice", markup=False)
            with Horizontal(classes="capability-actions"):
                yield Button("详情", id="capability-show")
                yield Button("启用/停用" if self.kind == "skills" else "刷新", id="capability-primary")
                yield Button("卸载" if self.kind == "skills" else "重连", id="capability-secondary")
            with Horizontal(classes="capability-actions"):
                yield Button("重载目录" if self.kind == "skills" else "重载配置", id="capability-reload")
                yield Button("返回 (Esc)", id="capability-close")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*(("名称", "状态", "来源") if self.kind == "skills" else ("名称", "状态", "工具")))
        self.refresh_entries()
        table.focus()

    def refresh_entries(self) -> None:
        selected = self.selected_id
        table = self.query_one(DataTable)
        table.clear()
        self._rows = {}
        for entry in self._entries():
            key = str(entry.get("id", entry["name"]))
            self._rows[key] = entry
            if self.kind == "skills":
                status = "停用" if not entry.get("enabled", True) else "已加载" if entry.get("loaded") else "可用"
                table.add_row(Text(str(entry["name"])), status, str(entry.get("scope", "")), key=key)
            else:
                state = str(entry.get("state", "未知"))
                table.add_row(Text(str(entry["name"])), MCP_STATE_LABELS.get(state, state),
                              str(entry.get("registered_tools", 0)), key=key)
        if selected in self._rows:
            table.move_cursor(row=list(self._rows).index(selected))
        self._update_selection()

    @property
    def selected_id(self) -> str | None:
        table = next(iter(self.query(DataTable)), None)
        if table is None or not table.row_count:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(DataTable.RowHighlighted, "#capability-table")
    def highlighted(self) -> None:
        self._update_selection()

    @on(DataTable.RowSelected, "#capability-table")
    def selected(self) -> None:
        self.show_selected()

    def _update_selection(self) -> None:
        entry = self._rows.get(self.selected_id)
        if entry is None:
            summary = "暂无技能" if self.kind == "skills" else "暂无 MCP 服务器"
        elif self.kind == "skills":
            summary = str(entry.get("description", ""))
        else:
            summary = f'{entry["name"]} · {entry.get("transport", "")} · {entry.get("registered_tools", 0)} 个工具'
            if entry.get("last_error"):
                summary += "\n" + str(entry["last_error"])
        self.query_one("#capability-summary", Static).update(summary)
        for button_id in ("show", "primary", "secondary"):
            self.query_one(f"#capability-{button_id}", Button).disabled = self._working or entry is None
        if entry is not None and self.kind == "skills":
            self.query_one("#capability-primary", Button).label = "停用" if entry.get("enabled", True) else "启用"

    def show_selected(self) -> None:
        key = self.selected_id
        if key is not None:
            try:
                text = self._show_detail(key)
            except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                self.query_one("#capability-notice", Static).update(str(exc))
            else:
                self.app.push_screen(ReadOnlyDetailsScreen(text))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        if button_id == "capability-close":
            self.action_close()
        elif button_id == "capability-show":
            self.show_selected()
        elif button_id == "capability-reload":
            self.apply_operation("reload", None)
        elif self.selected_id is not None:
            key = self.selected_id
            if button_id == "capability-primary":
                action = ("disable" if self._rows[key].get("enabled", True) else "enable") if self.kind == "skills" else "refresh"
            else:
                action = "unload" if self.kind == "skills" else "reconnect"
            self.apply_operation(action, key)

    @work(exclusive=True, exit_on_error=False)
    async def apply_operation(self, action: str, key: str | None) -> None:
        self._working = True
        self._update_selection()
        self.query_one("#capability-reload", Button).disabled = True
        self.query_one("#capability-notice", Static).update("正在处理…")
        try:
            result = self._operation(action, key)
            notice = await result if inspect.isawaitable(result) else result
        except (LanCherError, ValueError, RuntimeError, OSError) as exc:
            notice = str(exc)
        finally:
            self._working = False
        if self.is_mounted:
            self.query_one("#capability-notice", Static).update(notice)
            self.query_one("#capability-reload", Button).disabled = False
            self.refresh_entries()

    def action_close(self) -> None:
        self.dismiss(None)


class CapabilityCommands:
    def __init__(self, runner, notify, open_screen) -> None:
        self._runner = runner
        self._notify = notify
        self._open_screen = open_screen

    async def execute(self, kind: str, arguments: str) -> None:
        args = arguments.split()
        action = args[0] if args else "list"
        key = args[1] if len(args) > 1 else None
        if action == "list":
            caps = self._runner.capabilities
            entries = caps.list_skills if kind == "skills" else caps.mcp_status
            show = caps.show_skill if kind == "skills" else lambda selected: mcp_details(next(row for row in caps.mcp_status() if row["name"] == selected))
            self._open_screen(CapabilitiesScreen(kind, entries, show, lambda operation, selected: self.operate(kind, operation, selected)))
        elif action == "show":
            self._open_screen(ReadOnlyDetailsScreen(self._runner.capabilities.show_skill(key)))
        else:
            self._notify(await self.operate(kind, action, key), title="Skills" if kind == "skills" else "MCP")

    async def operate(self, kind: str, action: str, key: str | None) -> str:
        caps = self._runner.capabilities
        if kind == "skills":
            if action == "reload":
                return caps.reload_skills()
            if action == "unload":
                return caps.unload_skill(key)
            return caps.set_skill_enabled(key, action == "enable")
        if action == "reload":
            return await caps.reload_mcp()
        if action == "reconnect":
            return await caps.reconnect_mcp(key)
        return await caps.refresh_mcp(key)
