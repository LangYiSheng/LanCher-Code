from copy import deepcopy
from dataclasses import replace

import pytest

from lancher_code.contracts.messages import ChatRequest, ConversationMessage
from lancher_code.errors import ProviderRequestError
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.metadata import connection_fingerprint, parse_model_metadata
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.providers.responses import ResponsesProvider


def shared_request(config):
    return ChatRequest(model=config.model, system=['通用规则', '全局 Skills 目录', '项目环境'],
                       shared_system_count=2, prompt_cache_enabled=True,
                       messages=[ConversationMessage.text_message('user', '检查项目')])


def with_cache_support(config, supported):
    raw = {'id': config.model}
    if supported is not None:
        raw['capabilities'] = {'prompt_caching': {'explicit_breakpoints': {'supported': supported}}}
    metadata = parse_model_metadata(raw, fingerprint=connection_fingerprint(
        config.protocol, config.base_url, config.api_key), fetched_at='now')
    return replace(config, metadata=metadata)


def test_claude_caches_shared_prefix_and_growing_history_without_mutation(claude_provider_config):
    request = shared_request(claude_provider_config)
    original = deepcopy(request)
    provider = ClaudeProvider(claude_provider_config)
    first = provider._build_payload(request)
    assert request == original
    request.system[-1] = '另一个项目环境'
    request.messages.append(ConversationMessage.text_message('user', '继续检查'))
    second = provider._build_payload(request)
    assert first['system'][:2] == second['system'][:2]
    assert first['system'][1]['cache_control'] == {'type': 'ephemeral'}
    assert all('cache_control' not in block for block in first['system'][2:])
    assert first['cache_control'] == second['cache_control'] == {'type': 'ephemeral'}
    assert first['messages'] == second['messages'][:len(first['messages'])]


def test_claude_unknown_compatible_endpoint_receives_no_cache_fields(claude_provider_config):
    request = shared_request(claude_provider_config)
    request.prompt_cache_enabled = False
    payload = ClaudeProvider(claude_provider_config)._build_payload(request)
    assert payload['system'] == '\n\n'.join(request.system)
    assert 'cache_control' not in payload


@pytest.mark.parametrize('supported', [True, False, None])
def test_responses_uses_only_connection_bound_declared_cache_extension(openai_provider_config, supported):
    config = with_cache_support(openai_provider_config, supported)
    request = shared_request(config)
    original = deepcopy(request)
    provider = ResponsesProvider(config)
    payload = provider._build_payload(request)
    if supported is True:
        assert payload['input'][1]['content'] == [{
            'type': 'input_text', 'text': request.system[1], 'prompt_cache_breakpoint': {'mode': 'explicit'}}]
    else:
        assert payload['input'][1]['content'] == request.system[1]
    assert payload['input'][2]['content'] == '项目环境'
    assert 'prompt_cache_options' not in payload
    assert request == original
    stale = ResponsesProvider(replace(config, api_key='changed-credential'))._build_payload(request)
    assert stale['input'][1]['content'] == request.system[1]
    chat = OpenAIProvider(config)._build_payload(replace(request, api_mode='chat_completions'))
    assert chat['messages'][1]['content'] == request.system[1]
    assert 'prompt_cache_options' not in chat


@pytest.mark.parametrize('count', [True, -1, 4])
def test_invalid_shared_boundary_is_rejected_before_transport(claude_provider_config, count):
    request = shared_request(claude_provider_config)
    request.shared_system_count = count
    with pytest.raises(ProviderRequestError, match='缓存边界无效'):
        ClaudeProvider(claude_provider_config)._build_payload(request)
