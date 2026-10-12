"""HUD 的只读展示：输入状态快照，更新明确的一组控件。"""
from __future__ import annotations
from lancher_code.agent.service import AgentService
from dataclasses import dataclass
from collections.abc import Callable
from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static, Button
from lancher_code.providers.catalog import model_display_name
from lancher_code.usage.ledger import RunUsageSummary
from lancher_code.tui.usage import cache_ratio_label, usage_lines
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
    def __init__(self, agent: AgentService, provider_model: str,
                 command_allowed: Callable[[str], bool]) -> None:
        self.agent, self.provider_model = agent, provider_model
        self.command_allowed = command_allowed

    def refresh(self, widgets: HudWidgets, state: HudState) -> None:
        usage = self.agent.total_usage()
        activity = self.activity_label(state)
        execution = self.agent.execution_summary()
        banner = widgets.banner
        metrics = f'{banner.context_usage_status} · {cache_ratio_label(usage)}'
        action = state.busy_enter_action
        enter_action = {"follow_up": "排队", "steer": "补充", "draft": "草稿"}[action] if state.streaming else "发送"
        composer = widgets.composer
        is_command = composer.text.lstrip().startswith("/")
        if is_command:
            enter_action = "等待" if state.streaming and not self.command_allowed(composer.text) else "填入" if composer.slash_menu_active and composer.slash_enter_accepts else "执行"
        label = self.status_left_text()
        model, phase, policy = label.rsplit(" · ", 2)
        effort = self.agent.reasoning_effort
        preference = f' · {effort}' if effort != 'auto' else ''
        settings = f'{policy}{preference}'
        if state.width < 64:
            available = max(1, state.width - 4)
            identity_width = available if state.width < 48 else max(1, available - cell_len(activity) - 3)
            model = self.fit_text(model, max(1, identity_width - cell_len(f' · {settings}')))
            identity = self.fit_text(f'{model} · {settings}', identity_width)
            if state.width < 48:
                label = f'{identity}\n{self.fit_text(metrics, available)}\n{activity}'
            else:
                label = f'{identity} · {activity}\n{metrics}'
                label = '\n'.join(self.fit_text(line, available) for line in label.splitlines())
        else:
            hud_width = max(1, min(state.width, 112) - 4)
            available = hud_width - cell_len(metrics) - cell_len(activity) - 2
            model = self.fit_text(model, max(1, available - cell_len(f' · {settings}')))
            label = f'{model} · {settings}'
        colors = theme_palette(state.theme)
        summary = Text(label, style=colors["muted"])
        summary.stylize("bold " + colors["text"], 0, len(model))
        widgets.left.update(summary)
        widgets.center.update(metrics)
        widgets.right.update(activity)
        widgets.stage.update_phase(self.agent.work_phase, busy=state.streaming)
        details = (
            f"本次模型：{self.model_label()}\n"
            f"阶段：{phase} · 访问：{policy} · 推理：{effort}\n"
            f"工作目录：{self.agent.project_root}\n"
            f"会话：{self.agent.session_title or '新对话'}\n"
            f"当前{banner.context_usage_status} · {state.context_estimate_label}\n"
            f"会话累计已上报用量：\n{self.format_usage_text(usage)}\n"
        )
        if execution['running'] or execution['waiting']:
            details += f"托管进程：运行 {execution['running']} · 后台 {execution['background']} · 排队 {execution['waiting']} · /tasks 查看\n"
        if banner.mcp_status != 'MCP：未配置':
            details += banner.mcp_status
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
        model = self.model_label()
        policy = "只读" if self.agent.work_phase in {"discuss", "plan"} else {"project": "项目访问", "full": "完全访问"}[self.agent.execution_access]
        phase = {"discuss": "讨论", "plan": "计划", "execute": "执行"}[self.agent.work_phase]
        return f"{model} · {phase} · {policy}"

    def model_label(self) -> str:
        config = self.agent.model_config
        if config is not None and self.agent.model_ref:
            return model_display_name(config.providers, self.agent.model_ref)
        return self.provider_model

    @staticmethod
    def fit_text(text: str, width: int) -> str:
        rendered = Text(text)
        rendered.truncate(width, overflow='ellipsis')
        return rendered.plain

    def activity_label(self, state: HudState) -> str:
        """过程细节留在消息内，状态栏只表达当前需要关注的状态。"""
        hint = state.hint
        if self.agent.is_stopping:
            return '正在停止'
        if state.pending_permissions:
            return '等待确认'
        if self.agent.is_compacting:
            return '压缩上下文'
        if state.streaming:
            if '思考' in hint:
                return '正在思考'
            if '工具' in hint:
                return '执行工具'
            return '正在处理'
        if self.agent.queue_paused:
            return '队列暂停'
        if any(word in hint for word in ('失败', '未完成', '不可用')):
            return '未完成'
        if any(word in hint for word in ('停止', '取消')):
            return '已停止'
        return '就绪'

    @staticmethod
    def format_usage_text(usage: RunUsageSummary) -> str:
        return "\n".join(usage_lines(usage))
