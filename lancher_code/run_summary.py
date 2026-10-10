"""普通终端中的退出小结；展示结果，不负责停止任务或保存会话。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

from lancher_code.run_usage import RunUsageSummary
from lancher_code.session import SessionController


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
    existing = {item.session_id: item for item in session.list_sessions()}
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
    suffix = "（已上报统计）" if usage.incomplete_request_count else ""
    console.print(Text("本次启动用量" + suffix, style="bold"))
    for label, value, count in (
        ("输入", usage.input_tokens, usage.input_reported_request_count),
        ("输出", usage.output_tokens, usage.output_reported_request_count),
        ("缓存命中", usage.cached_input_tokens, usage.cache_reported_request_count),
    ):
        if usage.request_count and not count:
            text = "--（未提供）"
        else:
            text = f"{value:,} tokens"
            if count < usage.request_count:
                text += "（部分上报）"
        console.print(f"{label}：{text}", markup=False)
    total = ("--（未提供）" if usage.request_count and not (
        usage.input_reported_request_count and usage.output_reported_request_count
    ) else f"{usage.total_tokens:,} tokens")
    console.print("总计：" + total, markup=False)
    if usage.cache_hit_ratio is not None:
        ratio = f"{usage.cache_hit_ratio:.1%}"
    else:
        if not usage.request_count:
            ratio = "--（无请求）"
        elif not usage.incomplete_request_count and not usage.input_tokens:
            ratio = "--（输入为 0）"
        else:
            ratio = "--（未完整提供）"
    console.print("缓存比：" + ratio, markup=False)
    if usage.incomplete_request_count:
        console.print(f"有 {usage.incomplete_request_count} 次请求未返回完整用量，以上为已上报统计。", markup=False)
