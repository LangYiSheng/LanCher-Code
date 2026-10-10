"""普通终端中的退出小结；展示结果，不负责停止任务或保存会话。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from lancher_code.usage.ledger import RunUsageSummary
from lancher_code.sessions.controller import SessionController
from lancher_code.tui.usage import usage_lines


@dataclass(frozen=True, slots=True)
class ResumeTarget:
    session_id: str
    title: str


def select_resume_target(session: SessionController) -> ResumeTarget | None:
    """空白草稿不创建会话；只从本次实际访问且仍存在的会话中选择。"""
    ids = list(reversed(session.visited_session_ids))
    if session.session_id is not None:
        ids.insert(0, session.session_id)
    if not ids:
        return None
    existing = {item.session_id: item for item in session.list_sessions().items}
    for session_id in ids:
        if (item := existing.get(session_id)) is not None:
            return ResumeTarget(session_id, item.title)
    return None


def print_exit_summary(
    console: Console,
    *,
    project_root: Path,
    target: ResumeTarget | None,
    usage: RunUsageSummary,
    cleanup_errors: tuple[str, ...] = (),
    stopped_processes: int = 0,
) -> None:
    """调用者在屏幕恢复和全部收尾结束后调用一次。标题按纯文本输出。"""
    console.print()
    console.print(Text("今天也辛苦啦，LanCher 先下班咯～下次见 (｡･ω･｡)ﾉ", style="bold cyan"))
    if cleanup_errors:
        console.print(Text("退出收尾未全部完成：", style="bold yellow"))
        for error in cleanup_errors:
            console.print(Text("  " + error))
        console.print("已写入的记录仍保留；最新状态可能未完整保存。", markup=False)
    if target is not None:
        console.print()
        console.print(Text("对话：" + " ".join(target.title.split())))
        console.print(Text(f"下次在这个项目启动 LanCher Code 后，输入（项目：{project_root}）："))
        # 不截断命令；窄终端的自然换行不会丢失完整 UUID。
        console.print(Text(f"/session resume {target.session_id}", style="cyan"), soft_wrap=True)
    else:
        console.print("本次没有可恢复的已使用会话。", markup=False)
    if stopped_processes and not cleanup_errors:
        console.print(f"已收尾 {stopped_processes} 个托管进程；恢复对话后，服务需要重新启动。", markup=False)
    console.print()
    console.print(Text("本次启动已上报用量", style="bold"))
    for line in usage_lines(usage):
        console.print(line, markup=False)
