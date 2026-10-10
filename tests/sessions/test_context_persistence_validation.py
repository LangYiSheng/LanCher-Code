from __future__ import annotations

import copy

import pytest

from lancher_code.context.models import ContextManagementState
from lancher_code.context.tokens import estimate_request, update_usage_anchor
from lancher_code.contracts.messages import ChatRequest, ConversationMessage
from lancher_code.sessions.codec import SessionCodec
from lancher_code.usage.models import MessageUsage


def _encoded_context() -> tuple[ChatRequest, dict[str, object]]:
    request = ChatRequest(model="test", system=["system"],
                          messages=[ConversationMessage.text_message("user", "hello")])
    state = ContextManagementState()
    update_usage_anchor(state, request, MessageUsage(input_tokens=100, output_tokens=200))
    return request, SessionCodec._encode_context_management(state)


@pytest.mark.parametrize("field", ["token_count", "message_count"])
@pytest.mark.parametrize("invalid", [-1, True, False, 1.5, "100", None])
def test_restored_anchor_rejects_invalid_numeric_fields(field: str, invalid: object) -> None:
    _, encoded = _encoded_context()
    encoded["usage_anchor"][field] = invalid
    with pytest.raises(ValueError, match=field):
        SessionCodec._decode_context_management(encoded)


@pytest.mark.parametrize("field", ["system_tools_digest", "messages_digest"])
@pytest.mark.parametrize("invalid", ["", "shape", "x" * 64, "a" * 63, "a" * 65, 42, None])
def test_restored_anchor_requires_valid_snapshot_digest(field: str, invalid: object) -> None:
    _, encoded = _encoded_context()
    encoded["usage_anchor"][field] = invalid
    with pytest.raises(ValueError, match=field):
        SessionCodec._decode_context_management(encoded)


@pytest.mark.parametrize("invalid", [-1, True, False, "3", None, 1.5])
def test_restored_automatic_failure_count_rejects_coercion(invalid: object) -> None:
    _, encoded = _encoded_context()
    encoded["automatic_failure_count"] = invalid
    with pytest.raises(ValueError, match="automatic_failure_count"):
        SessionCodec._decode_context_management(encoded)


@pytest.mark.parametrize("invalid", [0, 1, "false", "true", None, []])
def test_restored_automatic_disabled_requires_boolean(invalid: object) -> None:
    _, encoded = _encoded_context()
    encoded["automatic_compaction_disabled"] = invalid
    with pytest.raises(ValueError, match="automatic_compaction_disabled"):
        SessionCodec._decode_context_management(encoded)


def test_valid_restored_anchor_preserves_reported_input_and_snapshot() -> None:
    request, encoded = _encoded_context()
    restored = SessionCodec._decode_context_management(copy.deepcopy(encoded))
    estimate = estimate_request(request, restored)
    assert estimate.source == "usage_calibrated"
    assert estimate.tokens == 100
    assert restored.usage_anchor.token_count == 100


def test_zero_input_and_empty_message_snapshot_can_be_restored() -> None:
    request = ChatRequest(model="test")
    state = ContextManagementState()
    update_usage_anchor(state, request, MessageUsage(input_tokens=0))
    restored = SessionCodec._decode_context_management(SessionCodec._encode_context_management(state))
    assert estimate_request(request, restored).tokens == 0


@pytest.mark.parametrize("version", [1, None, "2", 2.0, True])
def test_old_or_invalid_context_metering_version_is_rejected(version: object) -> None:
    _, encoded = _encoded_context()
    encoded["version"] = version
    with pytest.raises(ValueError, match="不兼容"):
        SessionCodec._decode_context_management(encoded)
