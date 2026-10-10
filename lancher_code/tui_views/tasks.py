"""Session 进程管理界面；只使用运行层接口，不直接操作操作系统进程。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from lancher_code.errors import LanCherError


@dataclass(frozen=True, slots=True)
class TaskScreenActions:
    """回调绑定打开弹窗时的 Session，切换对话不会改变任务归属。"""

    list_tasks: Callable[[], list[dict[str, object]]]
    read_output: Callable[[str, int], Awaitable[dict[str, object]]]
    stop: Callable[[str], Awaitable[object]]
    background: Callable[[str], Awaitable[object]]
    write_input: Callable[[str, str], Awaitable[object]]
    stop_session: Callable[[], Awaitable[object]]


PROCESS_STATUS = {
    "starting": "启动中", "running": "运行中", "stopping": "停止中",
    "exited": "已退出", "failed": "启动失败", "interrupted": "已中断",
    "lost": "已失联", "cancelled": "已取消",
}
TERMINAL_STATES = {"exited", "failed", "interrupted", "lost", "cancelled"}


def task_label(task: dict[str, object]) -> str:
    status = PROCESS_STATUS.get(str(task.get("status", "")), str(task.get("status", "未知")))
    lifetime = "会话后台" if task.get("lifetime") == "session" else "本轮"
    warning = " · 日志保存失败" if task.get("storage_error") else ""
    return f"{status} · {lifetime} · {task.get('description') or task.get('command') or '命令进程'}{warning}"


def elapsed_label(task: dict[str, object]) -> str:
    started = task.get("started_at")
    if not isinstance(started, str):
        return "尚未开始"
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(str(task["updated_at"])) if str(task.get("status")) in TERMINAL_STATES and task.get("updated_at") else datetime.now(timezone.utc)
        if start.tzinfo is None or end.tzinfo is None:
            return "耗时未知"
        seconds = max(0, int((end - start).total_seconds()))
        return f"{seconds // 3600}小时{seconds % 3600 // 60}分" if seconds >= 3600 else f"{seconds // 60}分{seconds % 60}秒" if seconds >= 60 else f"{seconds}秒"
    except (ValueError, TypeError):
        return "耗时未知"


class TasksScreen(ModalScreen[None]):
    """实时列表与有限日志缓冲；界面游标独立于模型的读取游标。"""

    BINDINGS = [("escape", "close", "返回")]
    CSS = """
    TasksScreen { align: center middle; background: $background 75%; }
    #tasks-box { width: 94%; max-width: 110; height: 94%; padding: 1 2; background: $surface; }
    #tasks-heading { height: 2; color: $text; text-style: bold; }
    #tasks-help { height: auto; color: $text-muted; }
    #tasks-table { height: 6; min-height: 3; background: $surface; }
    #tasks-detail { height: 5; max-height: 5; margin-top: 1; color: $text-muted; }
    #tasks-output-scroll { height: 1fr; min-height: 3; border: solid $panel; }
    #tasks-output { height: auto; padding: 0 1; }
    #tasks-error { height: auto; color: $error; }
    #tasks-input-row { height: 3; }
    #tasks-input { width: 1fr; }
    #tasks-input-send { width: 12; }
    #tasks-actions { height: 3; }
    #tasks-actions Button { width: 1fr; min-width: 5; border: none; color: $text-muted; background: transparent; }
    #tasks-actions Button:focus { background: $foreground; color: $background; }
    TasksScreen.-narrow #tasks-box { padding: 0 1; width: 100%; height: 100%; }
    TasksScreen.-narrow #tasks-actions { height: 6; layout: grid; grid-size: 3 2; }
    TasksScreen.-narrow #tasks-heading { height: 1; }
    TasksScreen.-narrow #tasks-help { display: none; }
    TasksScreen.-narrow #tasks-table { height: 3; min-height: 2; }
    TasksScreen.-narrow #tasks-detail { margin-top: 0; height: 2; max-height: 2; }
    TasksScreen.-narrow #tasks-output-scroll { min-height: 1; border: none; }
    """

    def __init__(self, session_id: str | None, actions: TaskScreenActions,
                 *, selected_process_id: str | None = None) -> None:
        super().__init__()
        self.session_id = session_id
        self.actions = actions
        self.selected_process_id = selected_process_id
        self._tasks: dict[str, dict[str, object]] = {}
        self._signature: tuple = ()
        self._cursor = 0
        self._output = ""
        self._refresh_lock = asyncio.Lock()
        self._action_busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-box"):
            yield Static("进程任务 · " + (self.session_id[:8] if self.session_id else "新对话"), id="tasks-heading", markup=False)
            yield Static("Esc 返回 · 停止本轮保留会话后台；停止会话会收尾全部进程", id="tasks-help", markup=False)
            yield DataTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
            yield Static("尚未选择进程", id="tasks-detail", markup=False)
            with VerticalScroll(id="tasks-output-scroll"):
                yield Static("", id="tasks-output", markup=False)
            yield Static("", id="tasks-error", markup=False)
            with Horizontal(id="tasks-input-row"):
                yield Input(placeholder="发送一行输入（末尾加换行）", id="tasks-input")
                yield Button("发送输入", id="tasks-input-send")
            with Horizontal(id="tasks-actions"):
                yield Button("刷新", id="tasks-refresh")
                yield Button("转后台", id="tasks-background")
                yield Button("停止进程", id="tasks-stop")
                yield Button("停止会话", id="tasks-stop-session")
                yield Button("返回", id="tasks-close")

    async def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("进程", "状态 / 归属", "用途")
        self.set_interval(0.5, self.refresh_tasks)
        self.call_after_refresh(self.refresh_tasks)

    def on_resize(self) -> None:
        self.set_class(self.size.width < 64, "-narrow")

    async def refresh_tasks(self) -> None:
        # 不堆积定时器：慢盘的一次刷新还没完成，就跳过后面的刷新。
        if self._refresh_lock.locked() or not self.is_mounted:
            return
        async with self._refresh_lock:
            try:
                tasks = self.actions.list_tasks()
                self._tasks = {str(item["process_id"]): item for item in tasks}
                signature = tuple((key, str(item)) for key, item in self._tasks.items())
                if signature != self._signature:
                    self._signature = signature
                    table = self.query_one(DataTable)
                    table.clear()
                    for process_id, task in self._tasks.items():
                        state = PROCESS_STATUS.get(str(task.get("status")), str(task.get("status", "未知")))
                        lifetime = "后台" if task.get("lifetime") == "session" else "本轮"
                        table.add_row(process_id[:8], f"{state} · {lifetime}", str(task.get("description") or task.get("command") or "命令"), key=process_id)
                    if not self.selected_process_id and self._tasks:
                        self.selected_process_id = next(iter(self._tasks))
                    if self.selected_process_id in self._tasks:
                        table.move_cursor(row=list(self._tasks).index(self.selected_process_id))
                await self._refresh_detail()
            except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                self._show_error(str(exc))

    @on(DataTable.RowSelected, "#tasks-table")
    async def choose_task(self, event: DataTable.RowSelected) -> None:
        event.stop()
        process_id = str(event.row_key.value)
        if process_id != self.selected_process_id:
            self.selected_process_id = process_id
            self._cursor = 0
            self._output = ""
        await self._refresh_detail()

    async def _refresh_detail(self) -> None:
        process_id = self.selected_process_id
        task = self._tasks.get(process_id or "")
        if task is None:
            self.query_one("#tasks-detail", Static).update("此会话还没有进程任务" if not self._tasks else "请选择一个进程")
            self._set_actions_enabled(False)
            return
        input_status = {"sent": "已接收", "sending": "接收中", "timeout": "超时", "failed": "失败", "cancelled": "已取消"}.get(str(task.get("input_status")), str(task.get("input_status")))
        receipt = f"输入：{input_status} · 累计 {task.get('input_bytes', 0)} 字节" if task.get("input_status") else ""
        exit_detail = f"退出码：{task.get('exit_code')} · 原因：{task.get('exit_reason') or '正常结束'}" if task.get("exit_code") is not None or task.get("exit_reason") else ""
        control_detail = " · ".join(part for part in (exit_detail, receipt) if part)
        self.query_one("#tasks-detail", Static).update(
            f"{task_label(task)}\nUUID：{process_id}\n"
            f"{str(task.get('transport', 'pipe')).upper()} · {elapsed_label(task)} · "
            f"就绪：{ {'unknown': '未知', 'pending': '等待', 'ready': '已就绪', 'timeout': '探测超时'}.get(str(task.get('readiness', 'unknown')), str(task.get('readiness'))) }\n目录：{task.get('cwd', '')}"
            + (f"\n{control_detail}" if control_detail else "")
        )
        active = str(task.get("status")) not in TERMINAL_STATES
        self._set_actions_enabled(active)
        running = task.get("status") == "running" and not self._action_busy
        self.query_one("#tasks-background", Button).disabled = not running or task.get("lifetime") == "session"
        self.query_one("#tasks-input-send", Button).disabled = not running
        self.query_one("#tasks-input", Input).disabled = not running
        page = await self.actions.read_output(process_id, self._cursor)
        if process_id != self.selected_process_id or not self.is_mounted:
            return
        output = str(page.get("text", page.get("output", "")))
        self._cursor = int(page.get("next_cursor", self._cursor))
        if output:
            view = self.query_one("#tasks-output-scroll", VerticalScroll)
            follow = not self._output or view.is_vertical_scroll_end
            self._output += output
            if len(self._output) > 48000:
                self._output = "[界面仅保留最近输出；磁盘日志仍保留]\n" + self._output[-46000:]
            # 极窄日志视口只有一行，末尾换行不能把最后的可读行挤出屏幕。
            # 完整输出与字符游标保持原样，仅去掉显示层末尾的空白行。
            self.query_one("#tasks-output", Static).update(Text(self._output.rstrip("\r\n")))
            if follow:
                self.call_after_refresh(view.scroll_end, animate=False)

    def _set_actions_enabled(self, active: bool) -> None:
        for widget_id in ("tasks-stop", "tasks-background", "tasks-input-send"):
            self.query_one(f"#{widget_id}", Button).disabled = not active or self._action_busy
        self.query_one("#tasks-input", Input).disabled = not active or self._action_busy
        self.query_one("#tasks-stop-session", Button).disabled = self.session_id is None or self._action_busy

    def _show_error(self, text: str) -> None:
        if self.is_mounted:
            self.query_one("#tasks-error", Static).update(text)

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        event.stop()
        action = (event.button.id or "").removeprefix("tasks-")
        if action == "close":
            self.action_close()
        else:
            self.execute_action(action)

    @on(Input.Submitted, "#tasks-input")
    def submit_input(self, event: Input.Submitted) -> None:
        event.stop()
        self.execute_action("input-send")

    @work(exclusive=False, group="tasks-action", exit_on_error=False)
    async def execute_action(self, action: str) -> None:
        if self._action_busy:
            return
        self._action_busy = True
        self._show_error("")
        try:
            process_id = self.selected_process_id
            if action == "refresh":
                await self.refresh_tasks()
            elif action == "stop-session":
                await self.actions.stop_session()
            elif process_id:
                if action == "stop":
                    await self.actions.stop(process_id)
                elif action == "background":
                    await self.actions.background(process_id)
                elif action == "input-send":
                    input_widget = self.query_one("#tasks-input", Input)
                    original = input_widget.value
                    if not original:
                        return
                    await self.actions.write_input(process_id, original + "\n")
                    if input_widget.value == original:
                        input_widget.value = ""
        except (LanCherError, ValueError, RuntimeError, OSError) as exc:
            self._show_error(str(exc))
        finally:
            self._action_busy = False
            if self.is_mounted:
                await self.refresh_tasks()

    def action_close(self) -> None:
        # 返回只关闭观察窗口，不停止已经托管的进程。
        self.dismiss(None)
