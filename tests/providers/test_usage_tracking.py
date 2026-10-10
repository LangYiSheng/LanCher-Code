from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lancher_code.context_management import _collect_summary
from lancher_code.errors import ProviderResponseError
from lancher_code.models import ChatRequest
from lancher_code.providers.factory import create_provider
from lancher_code.run_usage import RunUsageTracker


def _sse(value: dict) -> bytes:
    return ("data: " + json.dumps(value) + "\n\n").encode()


def _openai_usage(*, output: int = 3, cached: int | None = 6) -> bytes:
    usage = {"prompt_tokens": 10, "completion_tokens": output}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return _sse({"choices": [], "usage": usage})


def _claude_usage() -> bytes:
    return _sse({
        "type": "message_start",
        "message": {"usage": {
            "input_tokens": 10, "output_tokens": 0,
            "cache_read_input_tokens": 6, "cache_creation_input_tokens": 2,
        }},
    })


def _provider(config, tracker: RunUsageTracker, body: bytes | httpx.AsyncByteStream):
    def handler(_request):
        return httpx.Response(200, content=body) if isinstance(body, bytes) else httpx.Response(200, stream=body)

    return create_provider(
        config, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        usage_observer=tracker,
    )


@pytest.mark.asyncio
async def test_openai_observer_replaces_repeated_snapshots_and_preserves_missing_fields(openai_provider_config):
    tracker = RunUsageTracker()
    body = (
        _openai_usage()
        + _sse({"choices": [], "usage": {"completion_tokens": 4}})
        + _sse({"choices": [], "usage": {"completion_tokens": 4}})
        + b"data: [DONE]\n\n"
    )
    provider = _provider(openai_provider_config, tracker, body)
    events = [event async for event in provider.stream_chat(ChatRequest(model="first"))]
    snapshot = tracker.snapshot()
    assert snapshot.request_count == snapshot.completed_request_count == 1
    assert snapshot.total_tokens == 14
    assert snapshot.cached_input_tokens == 6
    assert snapshot.cache_hit_ratio == 0.6
    assert events[-1].usage.input_tokens == 10
    assert events[-1].usage.output_tokens == 4


@pytest.mark.asyncio
async def test_claude_observer_keeps_creation_and_reads_and_accepts_reported_zero(claude_provider_config):
    tracker = RunUsageTracker()
    body = (
        _claude_usage()
        + _sse({"type": "message_delta", "usage": {"output_tokens": 5}})
        + _sse({"type": "message_delta", "usage": {"output_tokens": 0, "cache_read_input_tokens": 0}})
        + _sse({"type": "message_stop"})
    )
    provider = _provider(claude_provider_config, tracker, body)
    events = [event async for event in provider.stream_chat(ChatRequest(model="first"))]
    snapshot = tracker.snapshot()
    assert snapshot.input_tokens == 12
    assert snapshot.output_tokens == snapshot.cached_input_tokens == 0
    assert snapshot.cache_hit_ratio == 0
    assert snapshot.incomplete_request_count == 0
    assert events[-1].usage.input_tokens == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("config_fixture", ["openai_provider_config", "claude_provider_config"])
async def test_partial_stream_cancellation_keeps_usage_already_received(request, config_fixture):
    config = request.getfixturevalue(config_fixture)
    reached_wait = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _openai_usage() if config.protocol == "openai" else _claude_usage()
            reached_wait.set()
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    tracker = RunUsageTracker()
    provider = _provider(config, tracker, WaitingStream())

    async def collect():
        return [event async for event in provider.stream_chat(ChatRequest(model="first"))]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(reached_wait.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = tracker.snapshot()
    assert snapshot.input_tokens == (10 if config.protocol == "openai" else 18)
    assert snapshot.cached_input_tokens == 6
    assert snapshot.completed_request_count == 0
    assert snapshot.incomplete_request_count == 1
    assert snapshot.cache_hit_ratio is None


@pytest.mark.asyncio
async def test_failed_stream_keeps_usage_and_missing_usage_is_not_zero(openai_provider_config):
    tracker = RunUsageTracker()
    provider = _provider(
        openai_provider_config, tracker,
        _openai_usage(cached=None) + _sse({"error": {"message": "failed after usage"}}),
    )
    with pytest.raises(ProviderResponseError):
        _ = [event async for event in provider.stream_chat(ChatRequest(model="first"))]
    snapshot = tracker.snapshot()
    assert snapshot.input_tokens == 10
    assert snapshot.output_tokens == 3
    assert snapshot.cache_reported_request_count == 0
    assert snapshot.incomplete_request_count == 1
    assert snapshot.cache_hit_ratio is None


@pytest.mark.asyncio
async def test_missing_usage_and_unterminated_stream_are_visible(openai_provider_config):
    tracker = RunUsageTracker()
    no_usage = _provider(openai_provider_config, tracker, b"data: [DONE]\n\n")
    _ = [event async for event in no_usage.stream_chat(ChatRequest(model="first"))]
    truncated = _provider(openai_provider_config, tracker, _openai_usage())
    _ = [event async for event in truncated.stream_chat(ChatRequest(model="second"))]
    snapshot = tracker.snapshot()
    assert snapshot.request_count == 2
    assert snapshot.completed_request_count == 1
    assert snapshot.input_reported_request_count == 1
    assert snapshot.incomplete_request_count == 2
    assert snapshot.cache_hit_ratio is None


@pytest.mark.asyncio
async def test_factory_shared_observer_includes_summary_and_model_requests(openai_provider_config, claude_provider_config):
    tracker = RunUsageTracker()
    summary_body = (
        _sse({"choices": [{"delta": {"content": "<summary>压缩内容</summary>"}}]})
        + _openai_usage() + b"data: [DONE]\n\n"
    )
    summary_provider = _provider(openai_provider_config, tracker, summary_body)
    text = await _collect_summary(summary_provider, ChatRequest(model="summary", allow_tool_calls=False))
    second_provider = _provider(claude_provider_config, tracker, _claude_usage() + _sse({"type": "message_stop"}))
    _ = [event async for event in second_provider.stream_chat(ChatRequest(model="second"))]
    snapshot = tracker.snapshot()
    assert text == "<summary>压缩内容</summary>"
    assert snapshot.request_count == 2
    assert snapshot.input_tokens == 28
    assert snapshot.output_tokens == 3
    assert snapshot.cached_input_tokens == 12
    assert snapshot.cache_hit_ratio == pytest.approx(12 / 28)


@pytest.mark.asyncio
async def test_invalid_usage_values_are_unknown_not_reported_zero(openai_provider_config):
    tracker = RunUsageTracker()
    body = _sse({"choices": [], "usage": {
        "prompt_tokens": True, "completion_tokens": -3,
        "prompt_tokens_details": {"cached_tokens": "0"},
    }}) + b"data: [DONE]\n\n"
    provider = _provider(openai_provider_config, tracker, body)
    _ = [event async for event in provider.stream_chat(ChatRequest(model="first"))]
    snapshot = tracker.snapshot()
    assert snapshot.input_reported_request_count == snapshot.output_reported_request_count == 0
    assert snapshot.cache_reported_request_count == 0
    assert snapshot.total_tokens == 0
    assert snapshot.incomplete_request_count == 1
