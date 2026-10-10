"""把同一份请求账本显示给用户；不推算缺失的消耗。"""
from __future__ import annotations

from lancher_code.usage.models import USAGE_FIELD_ATTRIBUTES
from lancher_code.usage.ledger import RunUsageSummary


def _reported_value(summary: RunUsageSummary, value: int, count: int, field: str) -> str:
    if summary.request_count and getattr(summary.usage, USAGE_FIELD_ATTRIBUTES[field]) is None:
        return "--（未提供）"
    text = f"{value:,} tokens"
    if count < summary.request_count or field in summary.usage.partial_fields:
        text += "（部分上报）"
    return text


def usage_lines(summary: RunUsageSummary) -> tuple[str, ...]:
    """缓存、缓存创建和推理是子项；总计始终只加输入与输出。"""
    lines = [
        "输入：" + _reported_value(summary, summary.input_tokens, summary.input_reported_request_count, "input"),
        "输出：" + _reported_value(summary, summary.output_tokens, summary.output_reported_request_count, "output"),
        "缓存命中：" + _reported_value(summary, summary.cached_input_tokens, summary.cache_reported_request_count, "cache"),
    ]
    if summary.usage.cache_creation_input_tokens is not None:
        lines.append("缓存创建（输入子项）：" + _reported_value(
            summary, summary.cache_creation_input_tokens, summary.cache_creation_reported_request_count, "cache_creation",
        ))
    if summary.usage.reasoning_output_tokens is not None:
        lines.append("推理（输出子项）：" + _reported_value(
            summary, summary.reasoning_output_tokens, summary.reasoning_reported_request_count, "reasoning",
        ))
    if (summary.request_count and summary.usage.input_tokens is None
            and summary.usage.output_tokens is None):
        total = "--（未提供）"
    else:
        total = f"{summary.total_tokens:,} tokens"
        if (summary.input_reported_request_count < summary.request_count
                or summary.output_reported_request_count < summary.request_count
                or {"input", "output"} & summary.usage.partial_fields):
            total += "（部分上报）"
    lines.append("总计：" + total)
    if summary.cache_hit_ratio is not None:
        ratio = f"{summary.cache_hit_ratio:.1%}"
    elif not summary.request_count:
        ratio = "--（无请求）"
    elif summary.invalid_request_count:
        ratio = "--（上报数据异常）"
    elif not summary.incomplete_request_count and not summary.input_tokens:
        ratio = "--（输入为 0）"
    else:
        ratio = "--（未完整提供）"
    lines.append("缓存比：" + ratio)
    if summary.incomplete_request_count:
        lines.append(f"有 {summary.incomplete_request_count} 次请求未返回完整用量，以上为已上报统计。")
    if summary.invalid_request_count:
        lines.append(f"有 {summary.invalid_request_count} 次请求上报数据异常；缓存比暂不可用。")
    return tuple(lines)
