from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lancher_code.context.compaction import _collect_summary
from lancher_code.errors import ProviderRequestError, ProviderResponseError
from lancher_code.contracts.messages import ChatRequest
from lancher_code.providers.models import ThinkingConfig
from lancher_code.providers.factory import create_provider
from lancher_code.usage.ledger import RunUsageTracker


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
async def test_openai_parses_native_cache_and_reasoning_without_double_counting(openai_provider_config):
    tracker = RunUsageTracker()
    body = _sse({"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {
        "prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 80,
        "completion_tokens_details": {"reasoning_tokens": 12},
    }}) + b"data: [DONE]\n\n"
    events = [event async for event in _provider(openai_provider_config, tracker, body).stream_chat(ChatRequest(model="first"))]
    assert events[-1].stop_reason == "length"
    assert events[-1].usage.is_final
    assert tracker.snapshot().total_tokens == 120
    assert tracker.snapshot().reasoning_output_tokens == 12
    assert tracker.snapshot().cached_input_tokens == 80


@pytest.mark.asyncio
async def test_claude_initial_zero_without_final_delta_is_not_final_usage(claude_provider_config):
    tracker = RunUsageTracker()
    body = _claude_usage() + _sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "回答"}})
    body += _sse({"type": "message_stop"})
    events = [event async for event in _provider(claude_provider_config, tracker, body).stream_chat(ChatRequest(model="first"))]
    assert events[-1].usage.output_tokens == 0
    assert not events[-1].usage.is_final
    assert tracker.snapshot().incomplete_request_count == 1
    assert tracker.snapshot().cache_hit_ratio is None


@pytest.mark.asyncio
async def test_claude_missing_normal_input_never_invents_total_from_cache(claude_provider_config):
    tracker = RunUsageTracker()
    body = _sse({"type": "message_start", "message": {"usage": {
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2,
    }}}) + _sse({"type": "message_delta", "usage": {"output_tokens": 3}, "delta": {"stop_reason": "max_tokens"}})
    body += _sse({"type": "message_stop"})
    events = [event async for event in _provider(claude_provider_config, tracker, body).stream_chat(ChatRequest(model="first"))]
    assert events[-1].usage.input_tokens is None
    assert events[-1].usage.cached_input_tokens == 5
    assert events[-1].usage.cache_creation_input_tokens == 2
    assert events[-1].stop_reason == "max_tokens"
    assert tracker.snapshot().input_reported_request_count == 0


@pytest.mark.asyncio
async def test_provider_callback_keeps_failed_usage_without_global_observer(openai_provider_config):
    records: list[dict[str, object]] = []
    request = ChatRequest(model="first", session_id="session", turn_id="turn", message_id="message",
                          purpose="compaction", run_id="run", request_id="request", usage_callback=records.append)
    body = _openai_usage() + _sse({"error": {"message": "failure"}})
    provider = create_provider(openai_provider_config,
                               client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                                   lambda _request: httpx.Response(200, content=body))))
    with pytest.raises(ProviderResponseError):
        _ = [event async for event in provider.stream_chat(request)]
    assert records[0]["status"] == "running"
    assert records[-1]["status"] == "failed"
    assert records[-1]["request_id"] == request.request_id
    assert records[-1]["request_id"] != "request"
    assert records[-1]["run_id"] == "run"
    assert records[-1]["session_id"] == "session"
    assert records[-1]["purpose"] == "compaction"
    assert records[-1]["usage"]["input_tokens"] == 10
    assert records[-1]["usage"]["is_final"] is False


@pytest.mark.asyncio
async def test_shared_provider_concurrent_requests_keep_separate_callbacks(openai_provider_config):
    records: list[dict[str, object]] = []
    tracker = RunUsageTracker()

    def handler(raw_request):
        model = json.loads(raw_request.content)["model"]
        count = 10 if model == "first" else 30
        return httpx.Response(200, content=_sse({"choices": [], "usage": {
            "prompt_tokens": count, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0},
        }}) + b"data: [DONE]\n\n")

    provider = create_provider(openai_provider_config,
                               client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                               usage_observer=tracker)

    async def collect(model):
        return [event async for event in provider.stream_chat(ChatRequest(
            model=model, session_id=model, usage_callback=records.append))]

    await asyncio.gather(collect("first"), collect("second"))
    finished = [record for record in records if record["status"] == "completed"]
    assert len(finished) == 2
    assert len({record["request_id"] for record in finished}) == 2
    assert {record["session_id"]: record["usage"]["input_tokens"] for record in finished} == {"first": 10, "second": 30}
    assert tracker.snapshot().input_tokens == 40
    assert provider._usage_requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("config_fixture", ["openai_provider_config", "claude_provider_config"])
async def test_output_cap_is_sent_to_actual_provider(request, config_fixture):
    config = request.getfixturevalue(config_fixture)

    def handler(raw_request):
        assert json.loads(raw_request.content)["max_tokens"] == 512
        body = b"data: [DONE]\n\n" if config.protocol == "openai" else _sse({"type": "message_stop"})
        return httpx.Response(200, content=body)

    provider = create_provider(config, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _ = [event async for event in provider.stream_chat(ChatRequest(model="first", max_output_tokens=512))]


def test_claude_rejects_thinking_budget_that_cannot_fit_output_cap(claude_provider_config):
    provider = create_provider(claude_provider_config)
    with pytest.raises(ProviderRequestError, match="思考"):
        provider._build_payload(ChatRequest(model="first", max_output_tokens=512,
                                            thinking=ThinkingConfig(enabled=True, budget_tokens=512)))


@pytest.mark.asyncio
@pytest.mark.parametrize("config_fixture", ["openai_provider_config", "claude_provider_config"])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_reusing_same_request_object_always_records_distinct_provider_attempts(
    request, config_fixture, concurrent,
):
    config = request.getfixturevalue(config_fixture)
    tracker = RunUsageTracker()
    records = []
    body = (_openai_usage() + b"data: [DONE]\n\n" if config.protocol == "openai"
            else _claude_usage() + _sse({"type": "message_delta", "usage": {"output_tokens": 2}})
            + _sse({"type": "message_stop"}))
    provider = _provider(config, tracker, body)
    model_request = ChatRequest(model="first", request_id="stale-id", session_id="session",
                                message_id="message", usage_callback=records.append)

    async def collect():
        return [event async for event in provider.stream_chat(model_request)]

    if concurrent:
        await asyncio.gather(collect(), collect())
    else:
        await collect()
        first_identity = model_request.request_id
        await collect()
        assert model_request.request_id != first_identity
    finished = [record for record in records if record["status"] == "completed"]
    assert len(finished) == 2
    assert len({record["request_id"] for record in finished}) == 2
    assert all(record["request_id"] != "stale-id" for record in finished)
    assert {record["session_id"] for record in finished} == {"session"}
    assert {record["message_id"] for record in finished} == {"message"}
    assert tracker.snapshot().request_count == 2
    assert tracker.snapshot().input_tokens == (20 if config.protocol == "openai" else 36)
    assert tracker.snapshot().output_tokens == (6 if config.protocol == "openai" else 4)
    assert provider._usage_requests == {}


@pytest.mark.asyncio
async def test_prepared_attempt_identity_is_consumed_once(openai_provider_config):
    tracker = RunUsageTracker()
    provider = _provider(openai_provider_config, tracker, _openai_usage() + b"data: [DONE]\n\n")
    model_request = ChatRequest(model="first", request_id="prepared")
    model_request.prepare_usage_attempt()
    _ = [event async for event in provider.stream_chat(model_request)]
    assert model_request.request_id == "prepared"
    _ = [event async for event in provider.stream_chat(model_request)]
    assert model_request.request_id != "prepared"
    assert {record.request_id for record in tracker.records} == {"prepared", model_request.request_id}


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
    second_provider = _provider(claude_provider_config, tracker, _claude_usage()
                                + _sse({"type": "message_delta", "usage": {"output_tokens": 0}})
                                + _sse({"type": "message_stop"}))
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
