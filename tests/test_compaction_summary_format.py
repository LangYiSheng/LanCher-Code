from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from lancher_code.context_budget import context_budget
from lancher_code.context_management import (
    SUMMARY_HEADINGS,
    SUMMARY_REQUEST_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    compact_transcript,
    parse_summary,
)
from lancher_code.context_tokens import estimate_request_tokens
from lancher_code.errors import ContextCompactionError, ProviderPromptTooLongError
from lancher_code.models import (
    CancellationToken,
    ChatRequest,
    ContextManagementState,
    ConversationMessage,
    MessageUsage,
    StreamEvent,
)


def _body() -> str:
    return "\n".join(f"## {heading}\n已记录重要事实。" for heading in SUMMARY_HEADINGS)


@pytest.mark.parametrize("wrapper", [
    "<summary>{body}</summary>",
    "以下是整理后的摘要：\n<summary>{body}</summary>\n请继续。",
    "\ufeff \n<SUMMARY >\n{body}\n</SUMMARY >\n",
    "```xml\n<summary>{body}</summary>\n```",
    "````markdown\n{body}\n````",
    "~~~markdown\n{body}\n~~~",
    "<summary>\n```markdown\n{body}\n```\n</summary>",
    "{body}",
])
def test_summary_accepts_common_wrappers_without_changing_content(wrapper: str) -> None:
    assert parse_summary(wrapper.format(body=_body())) == _body()


def test_summary_headings_are_lines_outside_code_fences() -> None:
    body = _body().replace("## 文件和代码段\n已记录重要事实。", """## 文件和代码段
正文提到 ## 当前工作 不属于标题。
```markdown
## 当前工作
### 待办任务
```
""")
    assert parse_summary(body) == body


@pytest.mark.parametrize("wrapped", [False, True])
def test_summary_can_quote_protocol_tags_as_code(wrapped: bool) -> None:
    body = _body().replace("## 错误与修复\n已记录重要事实。", """## 错误与修复
错误涉及 `<summary>` 和 ``</summary>``，也可能是 `<summary`。
```<summary>```
```xml
<summary>这是代码示例。</summary>
<summary
```
""")
    text = "<summary>" + body + "</summary>" if wrapped else body
    assert parse_summary(text) == body


def test_unpaired_inline_backtick_cannot_hide_later_sections() -> None:
    body = _body().replace("## 主要请求和意图\n已记录重要事实。", "## 主要请求和意图\n未配对的反引号 ` 只是原话。")
    body = body.replace("## 文件和代码段\n已记录重要事实。", "## 文件和代码段\n路径为 `server.py`。")
    assert parse_summary(body) == body


def test_summary_accepts_cosmetic_heading_numbers_and_closing_hashes() -> None:
    body = "\n".join(f"## {index}. {heading} ##\n无。" for index, heading in enumerate(SUMMARY_HEADINGS, 1))
    assert parse_summary(body) == body


@pytest.mark.parametrize("malformed", [
    "", "普通回答", "<summary></summary>",
    "<summary>{body}", "{body}</summary>",
    "<summary><summary>{body}</summary></summary>",
    "<summary>{body}</summary><summary>{body}</summary>",
    "<summary>{body}</summary>\n<summary",
    "<summary>{body}</summary>\n</summary",
    "{body}\n<summary",
    "{body}\n<summary invalid='true'>",
    "{body}\n未配对的反引号 `<summary",
    "<think>这是推理。</think>\n{body}",
    "```markdown\n{body}",
])
def test_summary_rejects_ambiguous_or_incomplete_structure(malformed: str) -> None:
    with pytest.raises(ContextCompactionError):
        parse_summary(malformed.format(body=_body()))


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_order", "empty", "wrong_level", "extra"])
def test_summary_rejects_incomplete_or_ambiguous_sections(change: str) -> None:
    sections = [f"## {heading}\n内容。" for heading in SUMMARY_HEADINGS]
    if change == "missing":
        del sections[3]
    elif change == "duplicate":
        sections.insert(3, sections[3])
    elif change == "wrong_order":
        sections[2], sections[3] = sections[3], sections[2]
    elif change == "empty":
        sections[3] = f"## {SUMMARY_HEADINGS[3]}\n"
    elif change == "wrong_level":
        sections[3] = sections[3].replace("## ", "### ")
    else:
        sections.append("## 其他事项\n内容。")
    with pytest.raises(ContextCompactionError):
        parse_summary("<summary>" + "\n".join(sections) + "</summary>")


class Provider:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[ChatRequest] = []

    async def stream_chat(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        for event in response:
            yield event


def _response(text: str) -> list[StreamEvent]:
    return [StreamEvent(kind="text_delta", text=text), StreamEvent(kind="message_end")]


def _history() -> list[ConversationMessage]:
    return [ConversationMessage.text_message("user", "旧材料" + "x" * 11_000),
            ConversationMessage.text_message("assistant", "旧任务已处理。"),
            ConversationMessage.text_message("user", "继续定位问题，保留原话。")]


async def _compact(provider, *, history=None, cancellation_token=None, request_factory=None):
    return await compact_transcript(provider=provider, model="test", transcript=history or _history(),
                                    visible_tools=[], state=ContextManagementState(), context_window=8192,
                                    cancellation_token=cancellation_token, request_factory=request_factory)


@pytest.mark.asyncio
async def test_local_normalization_uses_one_request_and_does_not_persist_summary_instruction() -> None:
    provider = Provider(_response("```markdown\n" + _body() + "\n```"))
    history = _history()
    saved = deepcopy(history)
    result = await _compact(provider, history=history)
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1].blocks[0].text == SUMMARY_REQUEST_PROMPT
    assert provider.requests[0].messages[:-1] == saved
    assert history == saved
    assert result.transcript[1].blocks[0].text == _body()
    assert all(SUMMARY_REQUEST_PROMPT not in block.text for message in result.transcript for block in message.blocks)


@pytest.mark.asyncio
async def test_bad_structure_regenerates_once_from_same_history_and_binds_each_real_request() -> None:
    provider = Provider(_response("普通回答"), _response(_body()))
    bound = []
    token = CancellationToken()

    def bind(request):
        bound.append(request)
        return request

    await _compact(provider, cancellation_token=token, request_factory=bind)
    first, second = provider.requests
    assert bound == provider.requests
    assert first is not second
    assert first.messages[:-1] == second.messages[:-1]
    assert "上次摘要结构校验失败" in second.messages[-1].blocks[0].text
    assert "普通回答" not in second.messages[-1].blocks[0].text
    assert first.max_output_tokens == second.max_output_tokens == context_budget(8192, purpose="compaction").output_tokens
    for request in bound:
        assert request.purpose == "compaction"
        assert request.tools == [] and not request.allow_tool_calls and request.thinking is None
        assert request.cancellation_token is token


@pytest.mark.asyncio
async def test_format_retry_is_bounded_even_after_input_length_retries() -> None:
    provider = Provider(ProviderPromptTooLongError("第一次输入过长"), _response("普通回答"), _response("仍然无效"))
    with pytest.raises(ContextCompactionError, match="重新生成后仍"):
        await _compact(provider)
    assert len(provider.requests) == 3


@pytest.mark.asyncio
async def test_format_retry_length_rejection_does_not_drop_more_history_or_retry_again() -> None:
    provider = Provider(_response("普通回答"), ProviderPromptTooLongError("重试输入过长"))
    with pytest.raises(ContextCompactionError, match="拒绝摘要格式重试"):
        await _compact(provider)
    assert len(provider.requests) == 2
    assert provider.requests[0].messages[:-1] == provider.requests[1].messages[:-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("failure", ["length", "max_tokens", "max_output_tokens", "usage_cap", "missing_end", "tool_call"])
async def test_completion_errors_never_trigger_another_format_retry(retry: bool, failure: str) -> None:
    events = _response("<summary>" + _body() + "</summary>")
    if failure in {"length", "max_tokens", "max_output_tokens"}:
        events[-1].stop_reason = failure
    elif failure == "usage_cap":
        events[-1].usage = MessageUsage(output_tokens=context_budget(8192, purpose="compaction").output_tokens)
    elif failure == "missing_end":
        events.pop()
    else:
        events.insert(0, StreamEvent(kind="tool_call_delta"))
    provider = Provider(*([_response("普通回答")] if retry else []), events)
    with pytest.raises(ContextCompactionError):
        await _compact(provider)
    assert len(provider.requests) == 1 + int(retry)


@pytest.mark.asyncio
async def test_token_cancelled_at_first_response_end_prevents_format_retry() -> None:
    token = CancellationToken()

    class CancellingProvider(Provider):
        async def stream_chat(self, request):
            async for event in super().stream_chat(request):
                yield event
            token.cancel()

    provider = CancellingProvider(_response("普通回答"))
    with pytest.raises(asyncio.CancelledError):
        await _compact(provider, cancellation_token=token)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_token_cancellation_closes_stream_before_compaction_returns() -> None:
    token = CancellationToken()
    closed = False

    class CancellingProvider:
        async def stream_chat(self, request):
            nonlocal closed
            try:
                token.cancel()
                yield StreamEvent(kind="text_delta", text="仍在传输的摘要")
            finally:
                closed = True

    with pytest.raises(asyncio.CancelledError):
        await _compact(CancellingProvider(), cancellation_token=token)
    assert closed


@pytest.mark.asyncio
async def test_format_retry_cannot_exceed_input_budget() -> None:
    budget = context_budget(8192, purpose="compaction")
    history = _history()
    request = ChatRequest(model="test", system=[SUMMARY_SYSTEM_PROMPT],
                          messages=[*history, ConversationMessage.text_message("user", SUMMARY_REQUEST_PROMPT)],
                          tools=[], allow_tool_calls=False, thinking=None,
                          max_output_tokens=budget.output_tokens, purpose="compaction")
    # 为首次请求留下极少余量，新增的重试反馈必须被真实预算检查拦住。
    current = estimate_request_tokens(request, ContextManagementState())
    history[0].blocks[0].text += "x" * (3 * (budget.input_limit - current - 2))
    assert estimate_request_tokens(request, ContextManagementState()) <= budget.input_limit
    provider = Provider(_response("普通回答"))
    bound = []
    with pytest.raises(ContextCompactionError, match="重试超出输入预算"):
        await _compact(provider, history=history, request_factory=lambda value: bound.append(value) or value)
    assert len(provider.requests) == len(bound) == 1
