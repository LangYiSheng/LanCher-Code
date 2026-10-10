from __future__ import annotations

from copy import deepcopy

import pytest

from lancher_code.context_budget import context_budget
from lancher_code.errors import ContextCompactionError
from lancher_code.models import MessageUsage, StreamEvent, ThinkingConfig, ToolDefinition
from lancher_code.run_usage import RunUsageTracker
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.session import SessionController
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.repository import SessionRepositoryError
from lancher_code.tools.core.executor import ToolExecutor
from lancher_code.tools.core.registry import ToolRegistry
from lancher_code.turn_runner import TurnRunner


class SummaryProvider:
    def __init__(self, text, *, stop_reason=None):
        self.text = text
        self.stop_reason = stop_reason
        self.requests = []

    async def stream_chat(self, request):
        self.requests.append(request)
        yield StreamEvent(kind="text_delta", text=self.text)
        yield StreamEvent(kind="message_end", stop_reason=self.stop_reason,
                          usage=MessageUsage(input_tokens=100, output_tokens=50, cached_input_tokens=0))


def _summary():
    from lancher_code.context_management import SUMMARY_HEADINGS
    return "<summary>" + "\n".join(f"## {heading}\n已整理。" for heading in SUMMARY_HEADINGS) + "</summary>"


def _long_history(session):
    session.create_user_message("旧任务材料" + "x" * 50_000)
    message = session.create_assistant_message()
    session.append_message_content(message.id, "旧材料已记录。")
    session.complete_message(message.id)
    session.create_user_message("继续处理，保留我的这一条原话。")


@pytest.mark.asyncio
@pytest.mark.parametrize("text,stop_reason", [("无效摘要", None), (_summary(), "length")])
@pytest.mark.parametrize("matching_shape", [True, False])
async def test_rejected_summary_retains_history_and_anchor_but_accounts_usage(
    openai_provider_config, text, stop_reason, matching_shape,
):
    tracker = RunUsageTracker()
    session = SessionController(openai_provider_config, usage_tracker=tracker)
    _long_history(session)
    request = session.build_request([], allow_tool_calls=matching_shape)
    session.update_context_usage(request, MessageUsage(input_tokens=20_000, output_tokens=100))
    previous_transcript = deepcopy(session.transcript)
    previous_anchor = deepcopy(session.context_state.usage_anchor)
    provider = SummaryProvider(text, stop_reason=stop_reason)

    with pytest.raises(ContextCompactionError):
        await session.compact_context(provider=provider, visible_tools=[], turn_id="turn")

    assert session.transcript == previous_transcript
    assert session.context_state.usage_anchor == previous_anchor
    assert session.usage_summary().total_tokens == tracker.snapshot().total_tokens == 150
    assert tracker.records[0].purpose == "compaction"
    assert tracker.records[0].turn_id == "turn"
    assert tracker.records[0].message_id is None


@pytest.mark.asyncio
async def test_complete_candidate_including_tools_rejects_summary_and_rolls_back(openai_provider_config):
    openai_provider_config.context_window = 8_000
    session = SessionController(openai_provider_config)
    session.create_user_message("旧材料" + "x" * 5_000)
    reply = session.create_assistant_message()
    session.append_message_content(reply.id, "已记录。")
    session.complete_message(reply.id)
    session.create_user_message("继续")
    previous = deepcopy(session.transcript)
    context = deepcopy(session.context_state)
    # 摘要正文能缩小，但完整请求的工具定义本身已装不进窗口。
    tools = [ToolDefinition(name="large_tool", description="x" * 25_000,
                            input_schema={"type": "object"})]
    provider = SummaryProvider(_summary())

    with pytest.raises(ContextCompactionError):
        await session.compact_context(provider=provider, visible_tools=tools)

    assert session.transcript == previous
    assert session.context_state == context
    assert session.usage_summary().total_tokens == 150


def test_context_details_are_read_only_for_runtime_anchor(openai_provider_config):
    session = SessionController(openai_provider_config)
    session.create_user_message("开始")
    request = session.build_request([], allow_tool_calls=True)
    session.update_context_usage(request, MessageUsage(input_tokens=1_000, output_tokens=20))
    anchor = deepcopy(session.context_state.usage_anchor)

    other_shape = session.build_request([], allow_tool_calls=False)
    assert session.context_estimate(other_shape).source == "estimated"
    assert session.context_state.usage_anchor == anchor
    assert session.context_estimate(request).tokens == 1_000
    assert session.context_estimate(request).source == "usage_calibrated"


def test_thinking_output_cap_and_input_budget_share_actual_allowance(claude_provider_config):
    claude_provider_config.context_window = 16_000
    claude_provider_config.thinking = ThinkingConfig(enabled=True, budget_tokens=8_000)
    session = SessionController(claude_provider_config)
    request = session.build_request([], allow_tool_calls=True)
    budget = context_budget(session.context_window, request.max_output_tokens)

    assert request.max_output_tokens > request.thinking.budget_tokens
    assert budget.output_tokens == request.max_output_tokens
    assert budget.input_limit + budget.output_tokens < session.context_window

    claude_provider_config.context_window = 4_000
    request = session.build_request([], allow_tool_calls=True)
    assert context_budget(session.context_window, request.max_output_tokens).input_limit == 0


def test_default_thinking_budget_is_shared_by_capacity_and_provider(claude_provider_config):
    claude_provider_config.context_window = 8_000
    claude_provider_config.thinking = ThinkingConfig(enabled=True)
    session = SessionController(claude_provider_config)
    request = session.build_request([], allow_tool_calls=True)
    payload = ClaudeProvider(claude_provider_config)._build_payload(request)

    assert payload['thinking']['budget_tokens'] == request.thinking.effective_budget_tokens == 2_048
    assert payload['max_tokens'] == request.max_output_tokens > 2_048


@pytest.mark.asyncio
async def test_rejected_full_candidate_counts_failures_on_restored_context(openai_provider_config, tmp_path):
    openai_provider_config.context_window = 8_000
    session = SessionController(openai_provider_config)
    session.create_user_message("旧材料" + "x" * 5_000)
    reply = session.create_assistant_message()
    session.append_message_content(reply.id, "已记录。")
    session.complete_message(reply.id)

    class LargeTool:
        definition = ToolDefinition(name="large_tool", description="x" * 25_000,
                                    input_schema={"type": "object"})

    registry = ToolRegistry()
    registry.register(LargeTool())
    provider = SummaryProvider(_summary())
    runner = TurnRunner(provider, session, registry, ToolExecutor(registry, cwd=tmp_path))

    for count in range(1, 4):
        events = [event async for event in runner.run_user_turn(f"继续 {count}")]
        assert events[-1].kind == "turn_failed"
        assert session.context_state.automatic_failure_count == count

    assert session.context_state.automatic_compaction_disabled is True
    assert len(provider.requests) == 3


@pytest.mark.parametrize("old_format", ["missing_ledger", "old_context"])
def test_old_accounting_format_cannot_silently_become_zero_usage(openai_provider_config, old_format):
    session = SessionController(openai_provider_config)
    session.create_user_message("保留旧记录")
    snapshot = session._snapshot()
    if old_format == "missing_ledger":
        del snapshot['state']['request_usage']
    else:
        snapshot['state']['context_management']['version'] = 1

    with pytest.raises(SessionRepositoryError, match="不兼容"):
        SessionCodec.decode(snapshot, session.session_id)
