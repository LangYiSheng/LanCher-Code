"""聊天的阶段、队列和计划控件，只发出交互事件。"""

from __future__ import annotations

import asyncio
from rich.markdown import Markdown
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class ChatAction(Message):
    def __init__(self, action: str, value: str = "") -> None:
        super().__init__()
        self.action = action
        self.value = value


class StageBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="stage-bar")

    def compose(self) -> ComposeResult:
        yield Button("讨论", id="phase-discuss", classes="quiet-action")
        yield Static("→", classes="stage-arrow")
        yield Button("计划", id="phase-plan", classes="quiet-action")
        yield Static("→", classes="stage-arrow")
        yield Button("执行", id="phase-execute", classes="quiet-action")
        yield Static("", id="phase-explanation", markup=False)

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(ChatAction("phase", (event.button.id or "").removeprefix("phase-")))

    def update_phase(self, phase: str, *, busy: bool) -> None:
        for value in ("discuss", "plan", "execute"):
            button = self.query_one(f"#phase-{value}", Button)
            button.set_class(phase == value, "-selected")
            button.disabled = busy
        self.query_one("#phase-explanation", Static).update(
            {"discuss": "只读调查", "plan": "制定计划 · 确认后执行", "execute": "按请求执行"}.get(phase, "")
        )


class PendingQueue(Vertical):
    def __init__(self) -> None:
        super().__init__(id="pending-queue")
        self._signature: tuple = ()
        self._render_lock = asyncio.Lock()

    async def update_items(self, items: list, *, paused: bool, busy: bool) -> None:
        async with self._render_lock:
            await self._render_items(items, paused=paused, busy=busy)

    async def _render_items(self, items: list, *, paused: bool, busy: bool) -> None:
        signature = (paused, busy, tuple((item.id, item.text, item.delivery, item.state) for item in items))
        if signature == self._signature:
            return
        self._signature = signature
        await self.remove_children()
        self.display = bool(items)
        if not items:
            return
        heading = Horizontal(classes="queue-heading")
        await self.mount(heading)
        await heading.mount(Static(f"待发送 {len(items)} 条" + (" · 已暂停" if paused else ""), markup=False))
        await heading.mount(Button("继续队列" if paused else "暂停队列", id="queue-toggle", classes="quiet-action"))
        for item in items:
            row = Vertical(classes="queue-item")
            await self.mount(row)
            kind = "补充当前任务" if item.delivery == "steer" else "下一轮"
            label = f"{kind} · {item.text}".replace("\n", " ↵ ")
            await row.mount(Static(label, markup=False, classes="queue-text"))
            actions = Horizontal(classes="queue-actions")
            await row.mount(actions)
            for action, text in (("edit", "编辑"), ("convert", "改为下一轮" if item.delivery == "steer" else "改为补充"), ("delete", "删除")):
                button = Button(text, classes="quiet-action")
                button.queue_id = item.id
                button.queue_action = action
                # 空闲时转为补充没有对应的当前任务。
                button.disabled = action == "convert" and item.delivery == "follow_up" and not busy
                await actions.mount(button)

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "queue-toggle":
            self.post_message(ChatAction("queue-toggle"))
        else:
            self.post_message(ChatAction(f"queue-{event.button.queue_action}", event.button.queue_id))


class PendingInputEditor(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "返回")]
    CSS = """
    PendingInputEditor { align: center middle; background: $background 70%; }
    #queue-editor { width: 85%; max-width: 90; height: 70%; background: $surface; padding: 1 2; }
    #queue-editor-title { height: 2; color: $primary; }
    #queue-editor-text { height: 1fr; border: solid $primary; }
    #queue-editor-actions { height: 3; }
    #queue-editor-actions Button { width: 1fr; min-width: 6; }
    #queue-editor-error { height: auto; color: $error; }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="queue-editor"):
            yield Static("编辑待发送内容 · 队列已暂停", id="queue-editor-title")
            yield TextArea(self.text, id="queue-editor-text", soft_wrap=True)
            yield Static("", id="queue-editor-error")
            with Horizontal(id="queue-editor-actions"):
                yield Button("保存", id="queue-editor-save", variant="primary")
                yield Button("返回", id="queue-editor-cancel")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "queue-editor-save":
            value = self.query_one(TextArea).text.strip()
            if not value:
                self.query_one("#queue-editor-error", Static).update("请保留一些内容，或返回后删除这一条。")
                return
            self.dismiss(value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanPanel(Vertical):
    def __init__(self) -> None:
        super().__init__(id="plan-panel")

    def compose(self) -> ComposeResult:
        yield Static("", id="plan-preview", markup=False)
        with Horizontal(classes="plan-actions"):
            yield Button("查看完整计划", id="plan-review", classes="quiet-action")
            yield Button("按此计划开始执行", id="plan-execute", classes="quiet-action")

    def update_snapshot(self, snapshot, *, busy: bool, phase: str) -> None:
        self.display = snapshot is not None and phase == "plan"
        if not self.display:
            return
        preview = "\n".join(snapshot.content.strip().splitlines()[:3])
        self.query_one("#plan-preview", Static).update("当前计划\n" + preview)
        self.query_one("#plan-execute", Button).disabled = busy or not snapshot.ready

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(ChatAction(event.button.id or ""))


class PlanReviewScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [("escape", "cancel", "返回")]
    CSS = """
    PlanReviewScreen { align: center middle; background: $background 70%; }
    #plan-review-box { width: 92%; height: 90%; background: $surface; padding: 1 2; }
    #plan-review-body { height: 1fr; }
    #plan-review-actions { height: 3; }
    #plan-review-actions Button { width: 1fr; min-width: 6; }
    """

    def __init__(self, session_id: str, snapshot, *, can_execute: bool) -> None:
        super().__init__()
        self.session_id = session_id
        self.snapshot = snapshot
        self.can_execute = can_execute

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-review-box"):
            yield Static("确认要执行的计划", classes="section-title")
            with VerticalScroll(id="plan-review-body"):
                yield Static(Markdown(self.snapshot.content))
            with Horizontal(id="plan-review-actions"):
                yield Button("按此计划执行", id="review-execute", disabled=not self.can_execute, variant="primary")
                yield Button("返回计划", id="review-cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss((self.session_id, self.snapshot.digest) if event.button.id == "review-execute" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PermissionPolicyScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "返回")]
    CSS = """
    PermissionPolicyScreen { align: center middle; background: $background 70%; }
    #policy-box { width: 64; max-width: 96%; height: auto; max-height: 90%; overflow-y: auto; padding: 1 2; background: $surface; }
    #policy-box Button { width: 1fr; height: 3; }
    #policy-box Static { height: auto; margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="policy-box"):
            yield Static("本次对话的审批策略", classes="section-title")
            yield Static("讨论只读，计划只写计划文档。审批策略不改变这些边界。")
            yield Button("逐次确认 · 修改和命令按规则询问", id="policy-default")
            yield Button("自动编辑 · 文件编辑自动允许", id="policy-acceptEdits")
            yield Button("跳过询问 · 仍遵循阶段与访问限制", id="policy-bypass")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss((event.button.id or "").removeprefix("policy-"))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReadOnlyDetailsScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "返回")]
    CSS = """
    ReadOnlyDetailsScreen { align: center middle; background: $background 70%; }
    #read-only-details { width: 92%; max-width: 96; height: 90%; padding: 1; background: $surface; }
    #read-only-scroll { height: 1fr; }
    #read-only-scroll Static { height: auto; }
    #read-only-close { height: 3; }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="read-only-details"):
            with VerticalScroll(id="read-only-scroll"):
                yield Static(self.text, markup=False)
            yield Button("返回 (Esc)", id="read-only-close")

    @on(Button.Pressed)
    def close_button(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
