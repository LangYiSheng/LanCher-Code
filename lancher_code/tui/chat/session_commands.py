"""会话命令控制：业务服务与明确的界面回调组合。"""
from __future__ import annotations
from collections.abc import Awaitable, Callable
from lancher_code.errors import LanCherError
from lancher_code.sessions.controller import SessionController
from lancher_code.sessions.paths import SessionPaths
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.agent.runner import TurnRunner
from lancher_code.tui.composer import ComposerTextArea
from lancher_code.tui.command_actions import CommandConfirmationScreen
from lancher_code.tui.chat_controls import ReadOnlyDetailsScreen

class SessionCommands:
    def __init__(self, session: SessionController, runner: TurnRunner, *,
                 composer: Callable[[], ComposerTextArea], busy: Callable[[], bool], notify: Callable,
                 open_screen: Callable, restore_view: Callable[[], Awaitable[None]],
                 refresh_completion: Callable[[], Awaitable[None]], refresh_queue: Callable[[], Awaitable[None]],
                 set_status: Callable[[str], None], preserve_input: Callable[[], None]) -> None:
        self.session, self.runner, self.composer, self.busy = session, runner, composer, busy
        self.notify, self.open_screen = notify, open_screen
        self.restore_view, self.refresh_completion, self.refresh_queue = restore_view, refresh_completion, refresh_queue
        self.set_status, self.preserve_input = set_status, preserve_input

    async def execute(self, arguments_text: str, *, confirmed: bool = False) -> None:
        arguments = arguments_text.split(maxsplit=2)
        action = arguments[0]
        if action == "stop":
            await self.runner.stop_session()
            self.set_status("当前会话已停止 · 草稿、队列和日志保留")
            await self.refresh_queue()
            self.notify("当前会话的本轮及全部托管进程已收尾。", title="Session")
            return
        if self.busy() or self.runner.has_active_turn:
            raise ValueError("本轮结束后才能管理会话，命令草稿已保留。")
        if action in {"archive", "remove"} and arguments[1] == self.session.session_id:
            raise SessionRepositoryError("当前会话不能归档或删除，请先运行 /session new。")
        if not confirmed and action in {"archive", "remove"}:
            paths = SessionPaths.for_session(self.session.project_root, arguments[1])
            if not paths.events.is_file():
                raise SessionRepositoryError("目标会话不存在，请输入完整 UUID。")
            target_title = "无法读取标题"
            try:
                target = next((item for item in self.session.list_sessions().items if item.session_id == arguments[1]), None)
                if target is not None:
                    target_title = target.title
            except (LanCherError, ValueError, OSError):
                if action != "remove":
                    raise
            description = (
                f"{'归档' if action == 'archive' else '删除'}项目会话：{target_title}\nUUID：{arguments[1]}"
                + ("\n删除包含对话记录、计划和会话工作文件，无法撤销。" if action == "remove" else "\n归档后记录和工作文件保留。")
            )
            session_id = self.session.session_id
            original_state = self.session.state
            composer = self.composer()
            original_text = composer.text
            consumed = False

            async def resolve(accepted: bool) -> None:
                nonlocal consumed
                if consumed:
                    return
                consumed = True
                if not accepted:
                    composer.focus()
                    return
                try:
                    if self.busy() or self.runner.has_active_turn or session_id != self.session.session_id or original_state is not self.session.state:
                        raise ValueError("当前会话状态已改变，请重新提交命令。")
                    await self.execute(arguments_text, confirmed=True)
                except (LanCherError, ValueError, RuntimeError, OSError) as exc:
                    self.notify(str(exc), title="命令未执行", severity="warning")
                else:
                    if composer.text == original_text:
                        composer.clear()
                await self.refresh_completion()
                composer.focus()

            self.preserve_input()
            self.open_screen(CommandConfirmationScreen(description, "/session " + arguments_text), resolve)
            return
        if action == "list" and len(arguments) == 1:
            listing = self.session.list_sessions()
            sessions = listing.items
            if not sessions:
                message = ("当前项目没有可恢复的会话。" if listing.issues else
                           "当前项目还没有会话；发送首条消息时自动创建。")
            else:
                active = self.session.session_id
                message = "\n".join(
                    f"{'* ' if item.session_id == active else '  '}{item.title} · {item.session_id[:8]}"
                    f"{' · 已归档' if item.archived else ''} · "
                    f"{item.updated_at.astimezone().strftime('%Y-%m-%d %H:%M')} · "
                    f"{item.message_count} 条消息 · {item.permission_rule_count} 条会话权限"
                    for item in sessions
                )
            if listing.issues:
                message += "\n\n不可恢复的会话（文件已保留）：\n" + "\n".join(
                    f"{issue.session_id} · {issue.message}" for issue in listing.issues)
            self.open_screen(ReadOnlyDetailsScreen("项目会话\n" + message))
            return

        if action == "new" and len(arguments) == 1:
            self.runner.new_session()
            await self.restore_view()
            self.notify("已打开新对话；发送首条消息时创建会话。", title="Session")
            return

        if action == "archive" and len(arguments) == 2:
            self.session.archive_session(arguments[1])
            self.notify(f"已归档会话：{arguments[1]}", title="Session")
            return

        if action == "remove" and len(arguments) == 2:
            self.session.remove_session(arguments[1])
            self.notify(f"已删除会话：{arguments[1]}", title="Session")
            return

        if action == "rename" and len(arguments) == 3:
            self.session.rename_session(arguments[1], arguments[2])
            self.set_status("")
            self.notify(f"会话标题已改为：{arguments[2]}", title="Session")
            return

        if action == "resume" and len(arguments) == 2:
            permission_count = self.runner.resume_session(arguments[1])
            await self.restore_view()
            notice = self.runner.model_notice
            self.notify(
                f"已恢复会话：{arguments[1]}（恢复 {permission_count} 条会话权限）" + (f"\n{notice}" if notice else ""),
                title="Session",
            )
            return

        raise SessionRepositoryError("参数不正确，请查看 /session 的命令提示。")
