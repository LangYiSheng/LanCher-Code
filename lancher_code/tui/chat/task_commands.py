"""任务窗口的回调在打开时绑定所属会话。"""
from __future__ import annotations
from collections.abc import Callable
from lancher_code.sessions.controller import SessionController
from lancher_code.agent.runner import TurnRunner
from lancher_code.tui.tasks import TaskScreenActions, TasksScreen

class TaskCommands:
    def __init__(self, session: SessionController, runner: TurnRunner, notify: Callable, open_screen: Callable) -> None:
        self.session, self.runner, self.notify, self.open_screen = session, runner, notify, open_screen

    async def execute(self, arguments_text: str) -> None:
        arguments = arguments_text.split()
        action = arguments[0] if arguments else "show"
        process_id = arguments[1] if len(arguments) > 1 else None
        session_id = self.session.session_id
        if action == "stop" and process_id:
            result = await self.runner.stop_process(process_id, session_id=session_id)
            storage_error = result.storage_error
            self.notify("进程已停止，但日志保存失败，请查看任务详情。" if storage_error else "进程及其托管子进程已停止，日志保留。", title="进程任务", severity="warning" if storage_error else "information")
            return
        if action == "background" and process_id:
            await self.runner.background_process(process_id, session_id=session_id)
            self.notify("已转交会话后台；停止本轮和切换对话后继续运行。", title="进程任务")
            return
        if process_id and process_id not in {str(item["process_id"]) for item in self.runner.list_processes(session_id=session_id)}:
            raise ValueError("当前会话没有此进程，请输入完整进程 UUID。")
        runner = self.runner
        async def read_output(target: str, cursor: int) -> dict[str, object]:
            return runner.read_process_output(target, cursor=cursor, max_chars=16000, session_id=session_id)
        # 所有回调绑定打开窗口时的 Session，不能跟随界面切换改归属。
        callbacks = TaskScreenActions(
            list_tasks=lambda: runner.list_processes(session_id=session_id),
            read_output=read_output,
            stop=lambda target: runner.stop_process(target, session_id=session_id),
            background=lambda target: runner.background_process(target, session_id=session_id),
            write_input=lambda target, text: runner.write_process_input(target, text, session_id=session_id),
            stop_session=lambda: runner.stop_session(session_id=session_id),
        )
        self.open_screen(TasksScreen(session_id, callbacks, selected_process_id=process_id))
