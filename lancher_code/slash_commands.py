"""命令定义、逐级补全及参数校验；不依赖终端控件。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlashCompletionContext:
    text: str
    session_ids: tuple[str, ...] = ()
    active_session_id: str | None = None
    process_choices: tuple[tuple[str, str], ...] = ()
    model_choices: tuple[tuple[str, str], ...] = ()
    active_model_ref: str | None = None
    default_model_ref: str | None = None
    permission_policy: str = "default"


@dataclass(frozen=True, slots=True)
class SlashCompletionCandidate:
    key: str
    value: str
    display: str
    description: str
    replace_start: int
    replace_end: int
    append_space: bool = False
    detail: str = ""
    optional: bool = False

    def apply(self, text: str) -> str:
        suffix = " " if self.append_space else ""
        return f"{text[:self.replace_start]}{self.value}{suffix}{text[self.replace_end:]}"


@dataclass(frozen=True, slots=True)
class SlashCommandDefinition:
    name: str
    description: str
    detail: str
    usage: str
    branch: bool = False


@dataclass(frozen=True, slots=True)
class SlashCommandMatch:
    definition: SlashCommandDefinition
    arguments_text: str


SESSION_ACTIONS = {
    "new": "开始新的对话，首条消息自动创建会话",
    "list": "列出此项目下所有的会话",
    "stop": "停止当前会话的本轮与全部后台进程",
    "resume": "按 UUID 恢复已有会话",
    "rename": "修改会话标题，UUID 保持不变",
    "archive": "归档已有会话",
    "remove": "删除会话及其工作文件",
}
TASK_ACTIONS = {
    "list": "查看当前会话的进程任务",
    "show": "打开进程详情与增量日志",
    "read": "读取当前进程输出",
    "stop": "停止一个进程及其托管子进程",
    "background": "把本轮进程交给会话后台继续运行",
}
POLICIES = {
    "default": "标准 · 修改和命令按规则询问",
    "acceptEdits": "自动编辑 · 文件编辑自动允许",
    "bypass": "跳过询问 · 仍遵守阶段与访问限制",
}
SETTINGS = {
    "theme": ("切换深浅主题", {"dark": "深色", "light": "浅色"}),
    "thinking": ("显示或隐藏思考记录", {"on": "显示", "off": "隐藏 · 不影响工具记录"}),
    "busy-enter": ("设置忙时 Enter 行为", {"follow_up": "排到下一轮", "steer": "补充当前任务", "draft": "仅保留草稿"}),
    "default-model": ("选择新对话默认模型", {}),
    "open": ("打开完整设置 · 连接、模型参数、MCP 与规则", {}),
}


def extract_exact_command_name(text: str) -> str | None:
    parts = text.lstrip().split(maxsplit=1)
    return parts[0][1:] if parts and parts[0].startswith("/") else None


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommandDefinition] = {}

    def register(self, definition: SlashCommandDefinition) -> None:
        if definition.name in self._commands:
            raise ValueError(f"命令已注册：{definition.name}")
        self._commands[definition.name] = definition

    def list_all(self) -> list[SlashCommandDefinition]:
        return list(self._commands.values())

    def get(self, name: str) -> SlashCommandDefinition | None:
        return self._commands.get(name)

    def suggest(self, prefix: str) -> list[SlashCommandDefinition]:
        query = prefix.casefold()
        return [item for item in self.list_all() if item.name.casefold().startswith(query) or query in item.description]

    def parse_submission(self, text: str) -> SlashCommandMatch | None:
        name = extract_exact_command_name(text)
        command = self.get(name or "")
        if command is None:
            return None
        return SlashCommandMatch(command, text.lstrip()[len(name) + 1:].strip())

    def complete(self, context: SlashCompletionContext) -> list[SlashCompletionCandidate]:
        text = context.text
        if not text.lstrip().startswith("/") or "\n" in text or "\r" in text:
            return []
        parts = text.split()
        name = parts[0][1:]
        command = self.get(name)
        trailing = text[-1].isspace()
        if len(parts) == 1 and not trailing and not (command and command.branch):
            start = len(text) - len(name)
            return [SlashCompletionCandidate(
                f"command:{c.name}", c.name, c.name, c.description, start, len(text),
                append_space=c.branch or c.name in {"discuss", "plan", "do"}, detail=c.detail,
            ) for c in self.suggest(name)]
        if command is None:
            return []
        args = parts[1:]
        # 精确输入父命令即可查看下一级；补全时补上必要的空格。
        exact_parent = len(parts) == 1 and not trailing
        completed = args if trailing or exact_parent else args[:-1]
        prefix = "" if trailing or exact_parent else args[-1]
        start = len(text) - len(prefix)
        options: list[tuple[str, str, bool, str, bool]] = []

        def add(value: str, label: str, space: bool = False, detail: str = "", optional: bool = False) -> None:
            options.append((value, label, space, detail, optional))

        if name == "session":
            if not completed:
                for value, label in SESSION_ACTIONS.items():
                    add(value, label, value not in {"new", "list", "stop"}, "作用范围：当前会话" if value == "stop" else "作用范围：当前项目")
            elif len(completed) == 1 and completed[0] in {"resume", "archive", "remove", "rename"}:
                for value in context.session_ids:
                    if completed[0] not in {"archive", "remove"} or value != context.active_session_id:
                        add(value, "项目会话" + (" · 当前" if value == context.active_session_id else ""), completed[0] == "rename", "使用完整 UUID；标题可重复。")
        elif name == "tasks":
            if not completed:
                for value, label in TASK_ACTIONS.items():
                    add(value, label, value != "list", "作用范围：当前会话；不创建新会话。")
            elif len(completed) == 1 and completed[0] in TASK_ACTIONS and completed[0] != "list":
                for value, label in context.process_choices:
                    add(value, label, detail="使用完整进程 UUID；所属会话保持不变。")
        elif name == "permissions" and not completed:
            for value, label in POLICIES.items():
                add(value, label + (" · 当前" if value == context.permission_policy else ""), detail="作用范围：本次对话；不改变工作阶段。")
        elif name == "model" and not completed:
            for value, label in context.model_choices:
                add(value, label + (" · 本次" if value == context.active_model_ref else "") + (" · 默认" if value == context.default_model_ref else ""), detail="只更改本次模型；新对话默认保持不变。")
        elif name == "settings":
            if not completed:
                for value, (label, _) in SETTINGS.items():
                    add(value, label, value != "open", "保存后立即生效；MCP 连接调整需重启。")
            elif len(completed) == 1 and completed[0] in SETTINGS:
                if completed[0] == "default-model":
                    for value, label in context.model_choices:
                        add(value, label + (" · 默认" if value == context.default_model_ref else ""), detail="只修改新对话默认模型；本次选择保持不变。")
                else:
                    for value, label in SETTINGS[completed[0]][1].items():
                        add(value, label, detail="保存界面偏好，不修改模型、权限或 MCP 文件。")
        return [SlashCompletionCandidate(
            f"argument:{name}:{start}:{value}", (" " if exact_parent else "") + value,
            value, label, start, len(text), space, detail, optional and (not prefix or prefix == value),
        ) for value, label, space, detail, optional in options
            if value.casefold().startswith(prefix.casefold()) or (prefix and prefix.casefold() in label.casefold())]

    def hint(self, text: str) -> str:
        match = self.parse_submission(text)
        if match is None:
            return "未知命令 · 输入 / 查看命令列表" if text.strip().startswith("/") and text.strip() != "/" else ""
        name, args = match.definition.name, match.arguments_text.split()
        if name in {"discuss", "plan", "do"}:
            return match.definition.description + " · Enter 切换；参数可选：任务描述"
        if name == "session":
            if not args:
                return "选择一个操作 · 作用范围：当前项目"
            if args[0] in {"resume", "archive", "remove", "rename"} and len(args) == 1:
                return "输入完整会话 UUID · 必填 · Tab 从项目会话中选择"
            if args[0] == "rename" and len(args) == 2:
                return "输入新的会话标题 · 必填 · 标题可以包含空格"
            if args[0] in {"archive", "remove"} and len(args) == 2:
                return "Enter 查看目标并确认 · 当前会话请先 /session new"
            if args == ["stop"]:
                return "Enter 停止本轮和当前会话的全部后台进程 · 日志保留"
        if name == "tasks":
            if not args:
                return "Enter 打开任务列表 · Tab 选择操作"
            if args[0] in TASK_ACTIONS and args[0] != "list" and len(args) == 1:
                return "输入完整进程 UUID · Tab 从当前会话任务中选择"
        if name == "model" and not args:
            return "选择本次模型 · 可输入名称或供应商 ID 检索"
        if name == "permissions" and not args:
            return "选择本次审批策略 · 不改变工作阶段"
        if name == "settings" and len(args) < 2 and args != ["open"]:
            return "选择设置项" if not args else "选择一个值 · 保存后生效"
        return match.definition.detail + " · Enter 执行"

    def advance_text(self, text: str) -> str | None:
        """自由输入参数没有候选项时，Tab 只推进参数位置。"""
        match = self.parse_submission(text)
        if match and match.definition.name == "session":
            args = match.arguments_text.split()
            if len(args) == 2 and args[0] == "rename" and not text[-1].isspace():
                return text + " "
        return None

    def validate(self, name: str, arguments: str) -> None:
        command = self.get(name)
        if command is None:
            raise ValueError("未知命令；输入 / 查看命令列表。")
        args = arguments.split()
        if name in {"discuss", "plan", "do"}:
            return
        if name in {"compact", "status", "exit"} and args:
            raise ValueError(f"用法：{command.usage}")
        if name == "permissions" and (len(args) > 1 or (args and args[0] not in POLICIES)):
            raise ValueError("请选择 default、acceptEdits 或 bypass。")
        if name == "model" and len(args) > 1:
            raise ValueError("请选择一个供应商 ID/模型 ID。")
        if name == "settings" and args:
            if args[0] not in SETTINGS:
                raise ValueError("未知设置项；输入 /settings 查看可用设置。")
            if args[0] == "open":
                if len(args) != 1:
                    raise ValueError("用法：/settings open")
            elif len(args) != 2:
                raise ValueError("请选择一个设置值。")
            elif args[0] != "default-model" and args[1] not in SETTINGS[args[0]][1]:
                raise ValueError("设置值无效；请从候选项中选择。")
        if name == "session" and args:
            action = args[0]
            counts = {"new": {1}, "list": {1}, "stop": {1}, "resume": {2}, "archive": {2}, "remove": {2}}
            valid = len(args) >= 3 if action == "rename" else action in counts and len(args) in counts[action]
            if not valid:
                raise ValueError("参数不完整或多余；" + self.hint("/session " + arguments))
        if name == "tasks" and args:
            if args[0] not in TASK_ACTIONS or len(args) != (1 if args[0] == "list" else 2):
                raise ValueError(f"用法：{command.usage}")


def create_default_slash_command_registry() -> SlashCommandRegistry:
    registry = SlashCommandRegistry()
    for name, label, detail, usage, branch in (
        ("discuss", "讨论想法 · 切换到讨论模式", "只读调查；不改变审批策略", "/discuss [问题]", False),
        ("plan", "制定计划 · 切换到计划模式", "调查并保存计划，确认后执行", "/plan [任务]", False),
        ("do", "开始执行 · 切换到执行模式", "保留当前审批策略", "/do [任务]", False),
        ("session", "切换对话 · 新建、恢复和管理会话", "首条消息自动保存；作用范围：当前项目", "/session <new|list|stop|resume|rename|archive|remove> [UUID] [标题]", True),
        ("tasks", "管理进程 · 输出、输入、后台和停止", "作用范围：当前会话；停止本轮会保留会话后台进程", "/tasks [list|show|read|stop|background] [进程UUID]", True),
        ("model", "选择模型 · 更改本次对话模型", "本次选择与新对话默认相互独立", "/model <供应商ID/模型ID>", True),
        ("permissions", "调整权限 · 更改本次审批策略", "只修改权限，不改变工作阶段", "/permissions <default|acceptEdits|bypass>", True),
        ("compact", "压缩上下文 · 整理当前对话记录", "作用范围：当前对话上下文", "/compact", False),
        ("settings", "修改设置 · 界面偏好与默认模型", "保存至配置；本次模型保持不变", "/settings <设置项> <值>", True),
        ("status", "查看状态 · 模型、用量与连接", "展开或收起 HUD 状态详情", "/status", False),
        ("exit", "退出程序 · 结束本次运行", "退出当前程序", "/exit", False),
    ):
        registry.register(SlashCommandDefinition(name, label, detail, usage, branch))
    return registry
