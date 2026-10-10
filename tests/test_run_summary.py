from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from lancher_code.models import MessageUsage
from lancher_code.run_summary import ResumeTarget, print_exit_summary, select_resume_target
from lancher_code.run_usage import RunUsageTracker
from lancher_code.session import SessionController


@pytest.fixture
def session(openai_provider_config, tmp_path):
    controller = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        yield controller
    finally:
        controller.close()


def create_session(controller, title):
    controller.create_user_message(title)
    controller.flush()
    return controller.session_id


def reported_usage(*, fields=frozenset({"input", "output", "cache"}), status="completed"):
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="openai", model="test-model")
    tracker.update_request(
        request_id,
        MessageUsage(input_tokens=12_340 if "input" in fields else None,
                     output_tokens=2_156 if "output" in fields else None,
                     cached_input_tokens=8_000 if "cache" in fields else None),
        provided_fields=fields,
    )
    tracker.finish_request(request_id, status=status)
    return tracker.snapshot()


def render_summary(*, usage=None, target=None, width=100, **kwargs):
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, force_terminal=False, color_system=None)
    print_exit_summary(
        console, project_root=Path("D:/项目目录"), target=target,
        usage=RunUsageTracker().snapshot() if usage is None else usage, **kwargs,
    )
    return buffer.getvalue()


def test_resume_target_uses_current_session_and_latest_title(session):
    first_id = create_session(session, "第一段对话")
    session.new_session()
    create_session(session, "第二段对话")
    session.resume_session(first_id)
    session.rename_session(first_id, "已经修改的标题")

    assert select_resume_target(session) == ResumeTarget(first_id, "已经修改的标题")


def test_blank_new_draft_falls_back_to_most_recently_used_session(session):
    create_session(session, "第一段对话")
    session.new_session()
    recent_id = create_session(session, "最后工作的对话")
    session.new_session()

    assert session.session_id is None
    assert select_resume_target(session) == ResumeTarget(recent_id, "最后工作的对话")
    assert session.session_id is None
    assert len(session.list_sessions()) == 2


def test_deleted_recent_session_falls_back_to_earlier_visited_session(session):
    earlier_id = create_session(session, "仍然存在的对话")
    session.new_session()
    removed_id = create_session(session, "刚刚删除的对话")
    session.new_session()
    session.remove_session(removed_id)

    assert select_resume_target(session) == ResumeTarget(earlier_id, "仍然存在的对话")


def test_no_target_when_all_visited_sessions_were_deleted(session):
    removed_id = create_session(session, "删掉这段对话")
    session.new_session()
    session.remove_session(removed_id)

    assert select_resume_target(session) is None
    assert session.session_id is None


def test_fresh_start_does_not_select_an_unvisited_historical_session(
    session, openai_provider_config, tmp_path, monkeypatch,
):
    historical_id = create_session(session, "上一次启动的对话")
    session.close()
    fresh = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        assert fresh.list_sessions()[0].session_id == historical_id
        monkeypatch.setattr(fresh, "list_sessions", lambda: pytest.fail("未使用 Session 时不扫描旧列表"))

        assert select_resume_target(fresh) is None
        assert fresh.session_id is None
        assert fresh.visited_session_ids == ()
    finally:
        fresh.close()


def test_resuming_a_session_counts_as_visited_without_a_new_message(
    session, openai_provider_config, tmp_path,
):
    historical_id = create_session(session, "只恢复来看看")
    session.close()
    fresh = SessionController(openai_provider_config, cwd=tmp_path)
    try:
        fresh.resume_session(historical_id)
        fresh.new_session()

        assert select_resume_target(fresh) == ResumeTarget(historical_id, "只恢复来看看")
    finally:
        fresh.close()


@pytest.mark.parametrize("width", [32, 100])
def test_summary_keeps_plain_title_and_full_resume_command_in_tiny_terminal(width):
    session_id = "99afd728580d4245871562151eef5259"
    title = "[bold red]可爱的服务[/bold red]\n继续这个对话"

    text = render_summary(target=ResumeTarget(session_id, title), width=width)
    joined = "".join(text.splitlines())

    # 自然换行可消耗词间空格，但不能把标题中的标记解析成样式。
    assert "".join(title.split()) in "".join(text.split())
    if width == 100:
        assert "[bold red]可爱的服务[/bold red]" in text
    assert "继续这个对话" in joined
    assert f"/session resume {session_id}" in text
    assert "启动 LanCher Code 后" in joined
    assert "D:\\项目目录" in joined
    assert "\x1b[" not in text


def test_complete_usage_prints_input_output_cache_total_and_weighted_ratio():
    text = render_summary(usage=reported_usage())

    assert "输入：12,340 tokens" in text
    assert "输出：2,156 tokens" in text
    assert "缓存命中：8,000 tokens" in text
    assert "总计：14,496 tokens" in text
    assert "缓存比：64.8%" in text
    assert "本次启动已上报用量" in text
    assert "已上报统计" not in text


def test_no_requests_is_known_zero_and_has_no_fabricated_resume_command():
    text = render_summary()

    assert "下次见" in text
    assert "本次没有可恢复的已使用会话" in text
    assert "/session resume" not in text
    assert "输入：0 tokens" in text
    assert "缓存比：--（无请求）" in text
    assert "未返回完整用量" not in text


def test_missing_usage_fields_are_unknown_instead_of_measured_zero():
    text = render_summary(usage=reported_usage(fields=frozenset({"input"})))

    assert "输入：12,340 tokens" in text
    assert "输出：--" in text
    assert "缓存命中：--" in text
    assert "总计：12,340 tokens（部分上报）" in text
    assert "输出：0 tokens" not in text
    assert "缓存命中：0 tokens" not in text
    assert "已上报统计" in text
    assert "有 1 次请求未返回完整用量" in text
    assert "缓存比：--" in text


def test_known_output_is_retained_when_input_is_not_reported():
    text = render_summary(usage=reported_usage(fields=frozenset({"output"}), status="cancelled"))

    assert "输入：--" in text
    assert "输出：2,156 tokens" in text
    assert "总计：2,156 tokens（部分上报）" in text
    assert "缓存比：--" in text
    assert "已上报统计" in text


def test_cancelled_request_with_reported_usage_still_warns_about_incomplete_total():
    text = render_summary(usage=reported_usage(status="cancelled"))

    assert "输入：12,340 tokens" in text
    assert "输出：2,156 tokens" in text
    assert "缓存比：--" in text
    assert "有 1 次请求未返回完整用量" in text
    assert "已上报统计" in text


def test_cleanup_failure_reports_uncertainty_without_claiming_all_processes_stopped():
    text = render_summary(
        target=ResumeTarget("99afd728580d4245871562151eef5259", "保留对话记录"),
        cleanup_errors=("会话保存失败：[test-error]", "连接关闭失败：测试连接"),
        stopped_processes=2,
    )

    assert "退出收尾未全部完成" in text
    assert "会话保存失败：[test-error]" in text
    assert "连接关闭失败：测试连接" in text
    assert "最新状态可能未完整保存" in text
    assert "已收尾 2 个托管进程" not in text
    assert "[test-error]" in text


def test_successful_process_cleanup_reminds_user_to_restart_services():
    text = render_summary(stopped_processes=2)

    assert "已收尾 2 个托管进程" in text
    assert "服务需要重新启动" in text


def test_summary_keeps_cache_creation_and_reasoning_as_subitems():
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="claude", model="test-model")
    tracker.update_request(request_id, MessageUsage(
        input_tokens=100, output_tokens=20, cached_input_tokens=60,
        cache_creation_input_tokens=10, reasoning_output_tokens=5,
    ), provided_fields=frozenset({"input", "output", "cache", "cache_creation", "reasoning"}))
    tracker.finish_request(request_id, status="completed")

    text = render_summary(usage=tracker.snapshot())

    assert "缓存创建（输入子项）：10 tokens" in text
    assert "推理（输出子项）：5 tokens" in text
    assert "总计：120 tokens" in text


def test_summary_hides_invalid_cache_ratio_and_reports_the_bad_snapshot():
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="openai", model="test-model")
    tracker.update_request(request_id, MessageUsage(
        input_tokens=100, output_tokens=20, cached_input_tokens=120,
    ), provided_fields=frozenset({"input", "output", "cache"}))
    tracker.finish_request(request_id, status="completed")

    text = render_summary(usage=tracker.snapshot())

    assert "缓存比：--（上报数据异常）" in text
    assert "120.0%" not in text
    assert "有 1 次请求上报数据异常" in text


def test_summary_marks_partial_totals_when_only_one_request_reported_input():
    tracker = RunUsageTracker()
    for usage in (MessageUsage(input_tokens=100, output_tokens=20, cached_input_tokens=0),
                  MessageUsage(output_tokens=30, cached_input_tokens=0)):
        request_id = tracker.start_request(protocol="openai", model="test-model")
        tracker.update_request(request_id, usage, provided_fields=usage.known_fields)
        tracker.finish_request(request_id, status="completed")

    text = render_summary(usage=tracker.snapshot())

    assert "输入：100 tokens（部分上报）" in text
    assert "输出：50 tokens" in text
    assert "总计：150 tokens（部分上报）" in text
    assert "缓存比：--" in text
