"""请求预算与上下文消耗分开：预算是容量约束，不能充当实际用量。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ContextBudget:
    output_tokens: int
    input_limit: int
    automatic_threshold: int
    recent_history_tokens: int
    tool_result_tokens: int
    tool_batch_tokens: int


def context_budget(
    context_window: int,
    max_output_tokens: int | None = None,
    *,
    purpose: str = "chat",
) -> ContextBudget:
    """按模型窗口分配预算，显式输出额度必须与实际请求保持一致。

    这里的安全余量覆盖粗估误差；软阈值提前启动整理，硬输入额度用于
    发出请求前检查。小窗口不会继承大模型的固定 20K 摘要预留。
    """
    window = max(1, context_window)
    default_output = min(4096, max(128, window // (6 if purpose == "compaction" else 8)))
    # 显式额度是实际请求上限，不能为了预算好看而偷偷缩小，否则检查的
    # 输入空间会与服务端实发请求不一致。无法容纳时输入预算归零，调用方
    # 在发出请求前给出明确错误。
    output = max_output_tokens if max_output_tokens is not None else max(1, min(default_output, max(1, window // 2)))
    if type(output) is not int or output <= 0:
        raise ValueError("输出 token 上限必须为正整数。")
    safety = max(1, min(1024, window // 32))
    input_limit = max(0, window - output - safety)
    threshold = max(0, input_limit - max(1, window // 12))
    return ContextBudget(
        output_tokens=output,
        input_limit=input_limit,
        automatic_threshold=threshold,
        recent_history_tokens=max(1, min(10_000, input_limit // 4)),
        tool_result_tokens=max(1, min(10_000, input_limit // 8)),
        tool_batch_tokens=max(1, min(40_000, input_limit // 3)),
    )
