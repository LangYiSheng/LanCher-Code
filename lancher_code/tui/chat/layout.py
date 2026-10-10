"""根据终端尺寸安排聊天子树；布局不读取应用运行状态。"""
from __future__ import annotations
from dataclasses import dataclass
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static
from lancher_code.tui.commands import SlashCompletionCandidate
from lancher_code.tui.composer import CommandHintBar, SlashCompletionMenu, ComposerTextArea
from lancher_code.tui.chat_controls import PendingQueue, PlanPanel
from lancher_code.tui.message import BannerWidget
from lancher_code.tui.permission import InlinePermissionPanel

@dataclass(frozen=True)
class ChatLayoutState:
    width: int
    height: int
    chat_started: bool
    pending_permissions: bool
    completion: SlashCompletionCandidate | None

def fit_chat_panels(root: Vertical, state: ChatLayoutState) -> None:
    short = state.height < 24
    session_completion = state.completion is not None and state.completion.presentation == "session"
    root.query_one("#composer-region").styles.max_height = "90%" if short and session_completion else "75%"
    root.query_one(CommandHintBar).styles.max_height = (4 if session_completion else 1) if short else None
    root.query_one(SlashCompletionMenu).styles.max_height = (3 if session_completion else 4) if short else 7
    approval = root.query_one("#approval-region", Vertical)
    queue = root.query_one(PendingQueue)
    plan = root.query_one(PlanPanel)
    if not short:
        root.query_one(BannerWidget).styles.height = "auto"
        root.query_one(BannerWidget).styles.margin = (0 if state.chat_started else 1, 2, 0, 2)
        root.query_one("#chat-view").styles.margin = (0 if state.chat_started else 1, 1, 0, 1)
        approval.styles.max_height = 12
        approval.styles.height = "auto"
        queue.styles.max_height = 8
        plan.styles.max_height = 6
        root.query_one("#plan-preview", Static).styles.max_height = 4
        for panel in root.query(InlinePermissionPanel):
            panel.set_compact(False)
            panel.styles.height = "auto"
            panel.styles.max_height = 12
        return
    root.query_one("#status-details-scroll", VerticalScroll).display = False
    root.query_one(BannerWidget).styles.height = 1
    root.query_one(BannerWidget).styles.margin = (0, 2, 0, 2)
    root.query_one("#chat-view").styles.margin = (0, 1, 0, 1)
    composer = root.query_one(ComposerTextArea)
    line_limit = 1 if state.pending_permissions else 3
    lines = max(1, min(line_limit, composer.wrapped_document.height))
    composer.styles.height = lines
    root.query_one("#composer").styles.height = lines + 1
    # 预留横幅、阶段、聊天、完整 HUD、输入边框与操作行。
    hud_extra = 1 if state.width < 48 else 0
    budget = max(3, state.height - 8 - lines - hud_extra)
    if session_completion and root.query_one(SlashCompletionMenu).display:
        # 完整 UUID 的提示优先保留；暂停队列可以缩到标题行，内容在自己的视口滚动。
        hint_width = max(1, min(state.width, 112) - 4)
        uuid_lines = (len(state.completion.value) + hint_width - 1) // hint_width
        budget -= 3 + min(4, 1 + uuid_lines)
    queue_height = min(3, max(1, budget)) if queue.display else 0
    queue.styles.max_height = max(1, queue_height)
    budget -= queue_height
    plan.styles.max_height = 2
    root.query_one("#plan-preview", Static).styles.max_height = 1
    if plan.display:
        budget -= 2
    permission_height = 3
    approval.styles.height = permission_height if state.pending_permissions else "auto"
    approval.styles.max_height = permission_height
    for panel in root.query(InlinePermissionPanel):
        panel.set_compact(True)
        panel.styles.height = permission_height
        panel.styles.max_height = permission_height
