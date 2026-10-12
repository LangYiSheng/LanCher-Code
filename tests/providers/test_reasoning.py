"""推理仅依据端点能力，不使用品牌、模型名和域名规则。"""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from lancher_code.contracts.messages import ChatRequest, ConversationMessage
from lancher_code.contracts.tools import ToolDefinition
from lancher_code.errors import ConfigError, ProviderResponseError
from lancher_code.providers.claude import ClaudeProvider
from lancher_code.providers.models import ProviderConfig
from lancher_code.providers.openai import OpenAIProvider
from lancher_code.providers.reasoning import reasoning_parameters, validate_effort
from lancher_code.providers.responses import ResponsesProvider
from lancher_code.providers.metadata import connection_fingerprint, parse_model_metadata, serialize_metadata
from lancher_code.config.loader import load_config_data
from lancher_code.providers.catalog import resolve_model


def _metadata(config, raw):
    return parse_model_metadata({'id': config.model, **raw}, fetched_at='test',
        fingerprint=connection_fingerprint(config.protocol, config.base_url, config.api_key))


@pytest.mark.parametrize('model', ['unbranded-alias', 'claude-sonnet-4-6', 'gpt-6-astra'])
def test_adaptive_thinking_is_driven_by_declaration_instead_of_model_name(model):
    config = ProviderConfig('claude', model, 'https://relay.test/v1', 'test')
    metadata = _metadata(config, {'capabilities': {'thinking': {'types': {'adaptive': {'supported': True}}},
        'effort': {'supported_levels': ['low', 'max']}}})
    for effort in ('auto', 'low', 'max'):
        payload = reasoning_parameters(protocol='claude', metadata=metadata, effort=effort, max_tokens=32768)
        assert payload['thinking'] == {'type': 'adaptive'}
        assert 'budget_tokens' not in payload['thinking']
        assert payload.get('output_config') == (None if effort == 'auto' else {'effort': effort})


@pytest.mark.parametrize('effort', ['none', 'off', 'minimal', True, None, [], 'xhigh'])
def test_declared_effort_range_has_explicit_validation(effort):
    config = ProviderConfig('claude', 'alias', 'https://relay.test', 'test')
    metadata = _metadata(config, {'effort': {'supported_levels': ['low', 'high']}})
    with pytest.raises(ConfigError, match='可选推理档位'):
        validate_effort('claude', metadata, effort)


@pytest.mark.parametrize('protocol, model, url', [('claude', 'claude-sonnet-4-6', 'https://api.anthropic.com'),
    ('openai', 'deepseek-flash', 'https://api.deepseek.com'), ('openai', 'gpt-6-astra', 'https://api.openai.com')])
def test_unknown_capability_uses_endpoint_default_without_name_or_host_inference(protocol, model, url):
    config = ProviderConfig(protocol, model, url, 'test')
    provider_cls = ClaudeProvider if protocol == 'claude' else OpenAIProvider
    payload = provider_cls(config)._build_payload(ChatRequest(model=model))
    assert not {'thinking', 'output_config', 'reasoning', 'reasoning_effort'} & payload.keys()
    assert 'input' not in payload


@pytest.mark.parametrize('protocol, provider_cls, field', [('claude', ClaudeProvider, 'output_config'),
    ('openai', OpenAIProvider, 'reasoning_effort'), ('openai', ResponsesProvider, 'reasoning')])
def test_compatible_effort_metadata_uses_protocol_fields(protocol, provider_cls, field):
    config = ProviderConfig(protocol, 'custom-alias', 'https://any-gateway.test/v1', 'test')
    config.metadata = _metadata(config, {'effort': {'supported_levels': ['low', 'high', 'max'], 'default_level': 'high'}})
    payload = provider_cls(config)._build_payload(ChatRequest(model=config.model, max_output_tokens=32768))
    assert payload[field] == ('high' if field == 'reasoning_effort' else {'effort': 'high'})
    assert 'thinking' not in payload


def test_explicit_known_false_capability_is_not_overridden_by_model_name():
    config = ProviderConfig('claude', 'claude-opus-5-5', 'https://api.anthropic.com', 'test')
    config.metadata = _metadata(config, {'capabilities': {'thinking': {'supported': False}, 'effort': {'supported': False}}})
    payload = ClaudeProvider(config)._build_payload(ChatRequest(model=config.model))
    assert 'thinking' not in payload and 'output_config' not in payload
    with pytest.raises(ConfigError):
        ClaudeProvider(config)._build_payload(ChatRequest(model=config.model, reasoning_effort='high'))


def test_missing_effort_metadata_preserves_explicit_user_choice_without_claiming_support():
    config = ProviderConfig('openai', 'custom', 'https://gateway.test/v1', 'test', reasoning_effort='high')
    assert OpenAIProvider(config)._build_payload(ChatRequest(model=config.model))['reasoning_effort'] == 'high'


def test_metadata_does_not_apply_to_different_request_model():
    config = ProviderConfig('claude', 'first', 'https://gateway.test/v1', 'test')
    config.metadata = _metadata(config, {'capabilities': {'thinking': {'types': {'adaptive': {'supported': True}}}}})
    assert 'thinking' not in ClaudeProvider(config)._build_payload(ChatRequest(model='second'))


def test_declared_empty_endpoints_do_not_fall_back_to_guessed_chat_interface():
    config = ProviderConfig('openai', 'unbranded', 'https://gateway.test/v1', 'test')
    config.metadata = _metadata(config, {'supported_endpoints': []})
    with pytest.raises(ConfigError, match='没有当前应用支持'):
        OpenAIProvider(config)._build_payload(ChatRequest(model=config.model))


def test_legacy_interface_budget_is_internal_and_bounded():
    config = ProviderConfig('claude', 'legacy-alias', 'https://gateway.test/v1', 'test')
    metadata = _metadata(config, {'capabilities': {'thinking': {'types': {'enabled': {'supported': True}}}}})
    for total, expected in [(2048, 1024), (4096, 2048), (32768, 8192)]:
        params = reasoning_parameters(protocol='claude', metadata=metadata, effort='auto', max_tokens=total)
        assert params == {'thinking': {'type': 'enabled', 'budget_tokens': expected}}
    with pytest.raises(ConfigError, match='2048'):
        reasoning_parameters(protocol='claude', metadata=metadata, effort='auto', max_tokens=1024)


@pytest.mark.asyncio
async def test_responses_routing_uses_declared_endpoint_instead_of_official_model_name():
    seen = []
    def handler(raw):
        seen.append(raw)
        payload = json.loads(raw.content)
        assert raw.url.path == '/v1/responses'
        assert payload['reasoning'] == {'effort': 'high'}
        assert payload['tools'][0]['name'] == 'read_file' and 'additional_tools' not in str(payload)
        terminal = {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
            {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'read_file', 'arguments': '{"path":"a.py"}'}]}}
        return httpx.Response(200, content=f'data: {json.dumps(terminal)}\n\n'.encode())
    config = ProviderConfig('openai', 'unbranded', 'https://gateway.test/v1', 'test')
    config.metadata = _metadata(config, {'supported_endpoints': ['responses'], 'effort': {
        'supported_levels': ['high'], 'default_level': 'high'}})
    provider = OpenAIProvider(config, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    request = ChatRequest(model=config.model, messages=[ConversationMessage.text_message('user', '读文件')],
        tools=[ToolDefinition('read_file', '读文件', {'type': 'object'})], max_output_tokens=32768)
    events = [event async for event in provider.stream_chat(request)]
    assert events[-1].response_complete and events[-1].assistant_blocks[0].call_id == 'call_1' and len(seen) == 1


@pytest.mark.asyncio
async def test_normal_responses_error_does_not_blame_experimental_setting():
    config = ProviderConfig('openai', 'custom', 'https://gateway.test/v1', 'test', api_mode='responses')
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={'error': {'message': 'invalid model'}}))
    provider = OpenAIProvider(config, client_factory=lambda: httpx.AsyncClient(transport=transport))
    with pytest.raises(ProviderResponseError) as error:
        _ = [event async for event in provider.stream_chat(ChatRequest(model=config.model))]
    assert '原生' not in str(error.value)


def test_thinking_display_preference_does_not_change_model_parameters():
    config = ProviderConfig('claude', 'custom', 'https://gateway.test/v1', 'test', reasoning_effort='max')
    metadata = _metadata(config, {'capabilities': {'thinking': {'types': {'adaptive': {'supported': True}}},
        'effort': {'supported_levels': ['low', 'max']}}})
    payloads = []
    for show in (False, True):
        app = load_config_data({'providers': {'p': {'name': '任意供应商', 'protocol': config.protocol,
            'base_url': config.base_url, 'api_key': config.api_key, 'models': {'m': {'model_name': config.model,
                'metadata': serialize_metadata(metadata), 'reasoning_effort': 'max'}}}},
            'default_model': 'p/m', 'ui': {'show_thinking_status': show}})
        resolved = resolve_model(app.providers, app.default_model)
        payloads.append(ClaudeProvider(resolved)._build_payload(ChatRequest(model=config.model)))
    assert payloads[0] == payloads[1] and payloads[0]['thinking'] == {'type': 'adaptive'}
