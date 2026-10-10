from dataclasses import FrozenInstanceError

import pytest

from lancher_code.usage.models import MessageUsage, add_usage, merge_usage
from lancher_code.usage.ledger import RequestUsageRecord, RunUsageTracker, summarize_records

ALL_FIELDS = frozenset({"input", "output", "cache"})


def test_empty_run_usage_does_not_invent_a_cache_ratio() -> None:
    snapshot = RunUsageTracker().snapshot()
    assert snapshot.total_tokens == 0
    assert snapshot.request_count == 0
    assert snapshot.incomplete_request_count == 0
    assert snapshot.cache_hit_ratio is None


def test_request_updates_replace_cumulative_snapshots_and_copy_values() -> None:
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="openai", model="first")
    usage = MessageUsage(input_tokens=100, output_tokens=10, cached_input_tokens=90)
    tracker.update_request(request_id, usage, provided_fields=ALL_FIELDS)
    tracker.update_request(request_id, usage, provided_fields=ALL_FIELDS)
    usage.input_tokens = 999
    tracker.finish_request(request_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.input_tokens == 100
    assert snapshot.total_tokens == 110
    assert snapshot.cache_hit_ratio == 0.9
    assert snapshot.incomplete_request_count == 0
    with pytest.raises(FrozenInstanceError):
        snapshot.input_tokens = 123


def test_multiple_requests_use_weighted_cache_ratio_across_models() -> None:
    tracker = RunUsageTracker()
    for model, usage in (
        ("first", MessageUsage(input_tokens=100, cached_input_tokens=90, output_tokens=10)),
        ("second", MessageUsage(input_tokens=10, cached_input_tokens=0, output_tokens=2)),
    ):
        request_id = tracker.start_request(protocol="openai", model=model)
        tracker.update_request(request_id, usage, provided_fields=ALL_FIELDS)
        tracker.finish_request(request_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.request_count == 2
    assert snapshot.input_tokens == 110
    assert snapshot.output_tokens == 12
    assert snapshot.cached_input_tokens == 90
    assert snapshot.cache_hit_ratio == pytest.approx(90 / 110)


@pytest.mark.parametrize("status", ["running", "cancelled", "failed", "incomplete"])
def test_unfinished_request_keeps_observed_tokens_without_claiming_complete_usage(status) -> None:
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="claude", model="first")
    tracker.update_request(
        request_id, MessageUsage(input_tokens=12, cached_input_tokens=5),
        provided_fields=frozenset({"input", "cache"}),
    )
    tracker.finish_request(request_id, status=status)
    snapshot = tracker.snapshot()
    assert snapshot.input_tokens == 12
    assert snapshot.cached_input_tokens == 5
    assert snapshot.output_reported_request_count == 0
    assert snapshot.incomplete_request_count == 1
    assert snapshot.cache_hit_ratio is None


def test_missing_cache_is_distinct_from_reported_zero() -> None:
    tracker = RunUsageTracker()
    first_id = tracker.start_request(protocol="openai", model="first")
    tracker.update_request(first_id, MessageUsage(input_tokens=10, output_tokens=0, cached_input_tokens=0), provided_fields=ALL_FIELDS)
    tracker.finish_request(first_id, status="completed")
    assert tracker.snapshot().cache_hit_ratio == 0
    second_id = tracker.start_request(protocol="openai", model="second")
    tracker.update_request(
        second_id, MessageUsage(input_tokens=10, output_tokens=0), provided_fields=frozenset({"input", "output"})
    )
    tracker.finish_request(second_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.cache_reported_request_count == 1
    assert snapshot.incomplete_request_count == 1
    assert snapshot.cache_hit_ratio is None


def test_zero_input_has_no_cache_ratio_and_other_fields_retain_coverage() -> None:
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="claude", model="first")
    tracker.update_request(request_id, MessageUsage(input_tokens=0, cached_input_tokens=0, is_final=False),
                           provided_fields=frozenset({"input", "cache"}))
    tracker.update_request(request_id, MessageUsage(output_tokens=2), provided_fields=frozenset({"output"}))
    tracker.finish_request(request_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.input_reported_request_count == snapshot.output_reported_request_count == 1
    assert snapshot.incomplete_request_count == 0
    assert snapshot.cache_hit_ratio is None


def test_merge_usage_preserves_unknown_fields_but_accepts_explicit_zero() -> None:
    merged = merge_usage(MessageUsage(input_tokens=100, output_tokens=5, cached_input_tokens=20, is_final=False),
                         MessageUsage(output_tokens=0, is_final=True))
    assert merged.input_tokens == 100
    assert merged.cached_input_tokens == 20
    assert merged.output_tokens == 0
    assert merged.is_final


def test_add_usage_preserves_known_subtotals_and_partial_coverage() -> None:
    usage = add_usage(MessageUsage(input_tokens=100, output_tokens=10, cached_input_tokens=20),
                      MessageUsage(output_tokens=5))
    assert usage.input_tokens == 100
    assert usage.output_tokens == 15
    assert usage.cached_input_tokens == 20
    assert {"input", "cache"} <= usage.partial_fields
    assert "output" not in usage.partial_fields
    assert not usage.is_complete
    assert MessageUsage.from_dict(usage.to_dict()) == usage


@pytest.mark.parametrize("data", [
    {"input_tokens": True}, {"output_tokens": -1}, {"input_tokens": "10"},
    {"partial_fields": ["made_up"]}, {"is_final": 1},
])
def test_usage_decode_rejects_invalid_data(data) -> None:
    with pytest.raises(ValueError):
        MessageUsage.from_dict(data)


def test_impossible_subcounts_remain_visible_and_disable_cache_ratio() -> None:
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="openai", model="first")
    tracker.update_request(request_id, MessageUsage(input_tokens=100, output_tokens=5, cached_input_tokens=120))
    tracker.finish_request(request_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.cached_input_tokens == 120
    assert snapshot.invalid_request_count == snapshot.incomplete_request_count == 1
    assert snapshot.cache_hit_ratio is None
    assert not snapshot.usage.is_valid
    mixed = add_usage(snapshot.usage, MessageUsage(input_tokens=1000, output_tokens=100, cached_input_tokens=0))
    assert not mixed.is_valid


def test_optional_subcounts_do_not_repeat_in_total_or_reduce_completeness() -> None:
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="claude", model="first")
    tracker.update_request(request_id, MessageUsage(input_tokens=100, output_tokens=10, cached_input_tokens=40,
                                                   cache_creation_input_tokens=20, reasoning_output_tokens=6))
    tracker.finish_request(request_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.total_tokens == 110
    assert snapshot.cache_creation_input_tokens == 20
    assert snapshot.reasoning_output_tokens == 6
    assert snapshot.cache_creation_reported_request_count == snapshot.reasoning_reported_request_count == 1
    assert snapshot.incomplete_request_count == 0


def test_partial_input_has_known_subtotal_but_no_reported_coverage_or_ratio() -> None:
    tracker = RunUsageTracker()
    identity = tracker.start_request(protocol="claude", model="first")
    tracker.update_request(identity, MessageUsage(input_tokens=100, cached_input_tokens=20, output_tokens=10,
                                                 partial_fields=frozenset({"input"})))
    tracker.finish_request(identity, status="completed")
    assert tracker.snapshot().input_tokens == 100
    assert tracker.snapshot().input_reported_request_count == 0
    assert tracker.snapshot().cache_hit_ratio is None


def test_serialized_records_retain_scope_and_do_not_import_another_run() -> None:
    tracker = RunUsageTracker(run_id="run-first")
    identity = tracker.start_request(protocol="openai", model="first", request_id="request-first",
                                     session_id="session", turn_id="turn", message_id="message", purpose="compaction")
    tracker.update_request(identity, MessageUsage(input_tokens=10, output_tokens=3, cached_input_tokens=0))
    tracker.finish_request(identity, status="completed")
    record = RequestUsageRecord.from_dict(tracker.records[0].to_dict())
    assert record.purpose == "compaction"
    assert record.session_id == "session"
    assert summarize_records([record]).total_tokens == 13
    record.usage.input_tokens = 20
    assert tracker.snapshot().input_tokens == 10
    tracker.accept_record(record)
    assert tracker.snapshot().input_tokens == 20
    with pytest.raises(ValueError, match="历史请求"):
        RunUsageTracker().accept_record(record)
    with pytest.raises(ValueError, match="复用"):
        tracker.start_request(protocol="openai", model="first", request_id=identity, session_id="another")


def test_explicit_existing_identity_is_idempotent_without_resetting_usage() -> None:
    tracker = RunUsageTracker()
    identity = tracker.start_request(protocol="openai", model="first", request_id="same")
    tracker.update_request(identity, MessageUsage(input_tokens=10))
    tracker.start_request(protocol="openai", model="first", request_id="same")
    assert tracker.snapshot().request_count == 1
    assert tracker.snapshot().input_tokens == 10
