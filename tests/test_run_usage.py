from dataclasses import FrozenInstanceError

import pytest

from lancher_code.models import MessageUsage
from lancher_code.run_usage import RunUsageTracker

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
    tracker.update_request(first_id, MessageUsage(input_tokens=10), provided_fields=ALL_FIELDS)
    tracker.finish_request(first_id, status="completed")
    assert tracker.snapshot().cache_hit_ratio == 0
    second_id = tracker.start_request(protocol="openai", model="second")
    tracker.update_request(
        second_id, MessageUsage(input_tokens=10), provided_fields=frozenset({"input", "output"})
    )
    tracker.finish_request(second_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.cache_reported_request_count == 1
    assert snapshot.incomplete_request_count == 1
    assert snapshot.cache_hit_ratio is None


def test_zero_input_has_no_cache_ratio_and_other_fields_retain_coverage() -> None:
    tracker = RunUsageTracker()
    request_id = tracker.start_request(protocol="claude", model="first")
    tracker.update_request(request_id, MessageUsage(), provided_fields=frozenset({"input", "cache"}))
    tracker.update_request(request_id, MessageUsage(output_tokens=2), provided_fields=frozenset({"output"}))
    tracker.finish_request(request_id, status="completed")
    snapshot = tracker.snapshot()
    assert snapshot.input_reported_request_count == snapshot.output_reported_request_count == 1
    assert snapshot.incomplete_request_count == 0
    assert snapshot.cache_hit_ratio is None
