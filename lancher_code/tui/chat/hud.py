"""HUD 的只读展示：输入状态快照，更新明确的一组控件。"""
from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Callable
from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static, Button
from lancher_code.sessions.controller import SessionController
from lancher_code.agent.runner import TurnRunner
from lancher_code.providers.catalog import model_display_name
from lancher_code.usage.ledger import RunUsageSummary
from lancher_code.tui.usage import usage_lines
from lancher_code.tui.message import BannerWidget
from lancher_code.tui.composer import ComposerTextArea
from lancher_code.tui.chat_controls import StageBar
from lancher_code.tui.theme import theme_palette

@dataclass(frozen=True)
class HudState:
    width: int
    theme: str
    hint: str
    streaming: bool
    pending_permissions: bool
    busy_enter_action: str
    context_estimate_label: str

@dataclass(frozen=True)
class HudWidgets:
    left: Static
    center: Static
    right: Static
    banner: BannerWidget
    composer: ComposerTextArea
    stage: StageBar
    details: Static
    model: Button
    policy: Button
    steer: Button
    queue: Button
    actions: Widget
    help: Static

class HudPresenter:
    def __init__(self, session: SessionController, runner: TurnRunner, provider_model: str,
                 command_allowed: Callable[[str], bool]) -> None:
        self.session, self.runner, self.provider_model = session, runner, provider_model
        self.command_allowed = command_allowed

    def refresh(self, widgets: HudWidgets, state: HudState) -> None:
        usage = self.session.total_usage()
        center_text = state.hint or ("正在处理" if state.streaming else "就绪")
        execution = self.runner.execution_summary()
        badges = []
        if execution["background"]:
            badges.append(f"后台 {execution['background']}")
        if execution["notifications"]:
            badges.append(f"通知 {execution['notifications']} /tasks")
        process_badges = " · ".join(badges)
        right_text = process_badges
        status_center = widgets.center
        status_right = widgets.right

        banner = widgets.banner
        estimate = banner.context_usage_status.replace("上下文 ", "预计 ")
        action = state.busy_enter_action
        enter_action = {"follow_up": "排队", "steer": "补充", "draft": "草稿"}[action] if state.streaming else "发送"
        composer = widgets.composer
        is_command = composer.text.lstrip().startswith("/")
        if is_command:
            enter_action = "等待" if state.streaming and not self.command_allowed(composer.text) else "填入" if composer.slash_menu_active and composer.slash_enter_accepts else "执行"
        compact_state = center_text.split(" · ", 1)[0]
        if state.pending_permissions:
            compact_state = "等待确认"
        elif self.runner.queue_paused:
            compact_state = "队列暂停"
        elif state.streaming:
            compact_state = "正在停止" if "停止" in compact_state else "处理中"
        compact_state = compact_state[:6]
        label = self.status_left_text()
        model, phase, policy = label.rsplit(" · ", 2)
        if state.width < 64:
            available = max(8, state.width - 4)
            model_limit = max(5, available - (10 if state.width < 48 else 28))
            if process_badges:
                model_limit = max(5, model_limit - cell_len(f" · {compact_state}"))
            model_text = Text(model)
            model_text.truncate(model_limit, overflow="ellipsis")
            model = model_text.plain
            if state.width < 48:
                first = f"{model} · {phase}" + (f" · {compact_state}" if process_badges else "")
                last = process_badges or f"{compact_state} · Enter {enter_action}"
                label = f"{first}\n{policy} · {estimate}\n{last}"
            else:
                first = f"{model} · {phase} · {policy}" + (f" · {compact_state}" if process_badges else "")
                last = process_badges or f"{compact_state} · Enter {enter_action}"
                label = f"{first}\n{estimate} · {last}"
        else:
            # 模型名按终端格宽截断，给阶段与权限保留位置。
            # 使用本轮状态的宽度，不能沿用状态变化前上一帧的布局。
            hud_width = max(1, min(state.width, 112) - 4)
            center_width = min(cell_len(f"{estimate} · {center_text}"), (hud_width * 35 + 99) // 100)
            available = hud_width - center_width - cell_len(right_text) - 1
            model_text = Text(model)
            model_text.truncate(max(1, available - cell_len(f" · {phase} · {policy}")), overflow="ellipsis")
            model = model_text.plain
            label = f"{model} · {phase} · {policy}"
        colors = theme_palette(state.theme)
        summary = Text(label, style=colors["muted"])
        summary.stylize("bold " + colors["text"], 0, len(model))
        phase_start = len(model) + 3
        summary.stylize(colors["primary"], phase_start, phase_start + len(phase))
        widgets.left.update(summary)

        status_center.update(f"{estimate} · {center_text}")
        status_right.update(right_text)
        widgets.stage.update_phase(self.session.work_phase, busy=state.streaming)
        config = self.runner.model_config
        ref = self.runner.model_ref
        notification_hint = " · /tasks 查看任务与输出" if execution["notifications"] else ""
        details = (
            f"本次模型：{self.status_left_text()}\n"
            f"工作目录：{self.session.project_root}\n"
            f"会话：{self.session.session_title or '新对话'} · {self.session.session_id or '首条消息后创建'}\n"
            f"会话工作目录：{self.session.paths.workspace if self.session.paths else '尚未创建'}\n"
            f"模型引用：{ref or self.provider_model}\n"
            f"新对话默认：{config.default_model if config is not None else '当前配置'}\n"
            f"会话累计已上报用量：\n{self.format_usage_text(usage)}\n"
            f"当前{banner.context_usage_status} · {state.context_estimate_label}\n"
            f"托管进程：运行 {execution['running']} · 会话后台 {execution['background']} · 排队 {execution['waiting']}\n"
            f"未读完成通知：{execution['notifications']}{notification_hint}\n"
            f"{banner.mcp_status}"
        )
        widgets.details.update(details)
        for button in (widgets.model, widgets.policy):
            button.disabled = state.streaming
        widgets.steer.display = state.streaming
        widgets.queue.display = state.streaming
        widgets.actions.set_class(state.streaming, "-working")
        busy_help = {"follow_up": "Enter 排下一轮", "steer": "Enter 补充当前任务", "draft": "Enter 保留草稿"}[action]
        widgets.help.update(
            ("本轮结束后执行 · 草稿已保留" if state.streaming and not self.command_allowed(composer.text) else f"Enter {enter_action} · Tab 补全 · Esc 关闭") if is_command else
            busy_help + " · Ctrl+Enter 补充 · Esc 停本轮" if state.streaming else "Enter 发送 · Shift+Enter 换行"
        )

    def status_left_text(self) -> str:
        config = self.runner.model_config
        if config is not None and self.runner.model_ref:
            model = model_display_name(config.providers, self.runner.model_ref)
        else:
            model = self.provider_model
        policy = {"default": "逐次确认", "acceptEdits": "自动编辑", "bypass": "跳过询问"}[self.session.permission_policy]
        phase = {"discuss": "讨论", "plan": "计划", "execute": "执行"}[self.session.work_phase]
        return f"{model} · {phase} · {policy}"

    @staticmethod
    def format_usage_text(usage: RunUsageSummary) -> str:
        return "\n".join(usage_lines(usage))
