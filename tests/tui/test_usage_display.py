import pytest

from lancher_code.usage.models import MessageUsage
from lancher_code.usage.ledger import RunUsageTracker
from lancher_code.tui.chat.hud import HudPresenter
from lancher_code.tui.usage import usage_lines


def summary_for(*usages: MessageUsage):
    tracker = RunUsageTracker()
    for usage in usages:
        request_id = tracker.start_request(protocol="openai", model="test-model")
        tracker.update_request(request_id, usage)
        tracker.finish_request(request_id, status="completed")
    return tracker.snapshot()


def test_unknown_fields_are_distinct_from_reported_zero_in_details():
    text = HudPresenter.format_usage_text(summary_for(
        MessageUsage(input_tokens=0, cached_input_tokens=0),
    ))

    assert "输入：0 tokens" in text
    assert "缓存命中：0 tokens" in text
    assert "输出：--（未提供）" in text
    assert "总计：0 tokens（部分上报）" in text


def test_known_partial_field_is_retained_when_no_request_has_a_complete_field():
    summary = summary_for(MessageUsage(
        input_tokens=12, output_tokens=3, cached_input_tokens=5,
        partial_fields=frozenset({"input"}),
    ))
    text = HudPresenter.format_usage_text(summary)

    assert summary.input_reported_request_count == 0
    assert "输入：12 tokens（部分上报）" in text
    assert "总计：15 tokens（部分上报）" in text
    assert "缓存比：--" in text


def test_details_and_terminal_use_the_same_usage_format():
    summary = summary_for(
        MessageUsage(input_tokens=100, output_tokens=20, cached_input_tokens=60,
                     cache_creation_input_tokens=10, reasoning_output_tokens=5),
        MessageUsage(output_tokens=7),
    )

    text = HudPresenter.format_usage_text(summary)

    assert text == "\n".join(usage_lines(summary))
    assert "总计：127 tokens（部分上报）" in text
    assert "缓存创建（输入子项）：10 tokens（部分上报）" in text
    assert "推理（输出子项）：5 tokens（部分上报）" in text


def test_initial_snapshot_is_labelled_as_incomplete_even_if_numbers_are_known():
    text = HudPresenter.format_usage_text(summary_for(MessageUsage(
        input_tokens=10, output_tokens=0, cached_input_tokens=0, is_final=False,
    )))

    assert "输入：10 tokens" in text
    assert "有 1 次请求未返回完整用量" in text
    assert "缓存比：--" in text


@pytest.mark.parametrize(("usage", "total"), [
    (MessageUsage(input_tokens=100, is_final=False), "总计：100 tokens（部分上报）"),
    (MessageUsage(output_tokens=30), "总计：30 tokens（部分上报）"),
    (MessageUsage(), "总计：--（未提供）"),
])
def test_total_keeps_the_known_subtotal_without_guessing_missing_fields(usage, total):
    text = "\n".join(usage_lines(summary_for(usage)))

    assert total in text
    assert "有 1 次请求未返回完整用量" in text
