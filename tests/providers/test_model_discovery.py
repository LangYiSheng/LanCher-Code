from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from lancher_code.errors import ConfigError, ProviderAuthError, ProviderRequestError, ProviderResponseError
from lancher_code.providers.discovery import ModelDiscoveryClient, discovery_connection
from lancher_code.providers.metadata import ModelCapabilities, connection_fingerprint, parse_model_metadata, serialize_metadata, load_metadata
from lancher_code.providers.models import ModelDefinition, ProviderDefinition
from lancher_code.providers.catalog import merge_discovered_models, resolve_model


def _provider(protocol='openai', base_url='https://gateway.test/v1'):
    return ProviderDefinition('自定义端点', protocol, base_url, 'secret-key')


def _client(handler, **options):
    return ModelDiscoveryClient(client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs), **options)


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,url,header', [('openai', 'https://gateway.test/v1/models', 'authorization'),
                                                ('claude', 'https://gateway.test/v1/models', 'x-api-key'),
                                                ('claude', 'https://gateway.test/custom/models', 'x-api-key')])
async def test_protocol_discovery_uses_configured_connection_without_brand_rules(protocol, url, header):
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url) == url
        assert 'secret-key' in request.headers[header]
        other = 'x-api-key' if header == 'authorization' else 'authorization'
        assert other not in request.headers
        return httpx.Response(200, json={'data': [{'id': 'arbitrary-alias', 'owned_by': 'owner', 'created': 123}]})
    result = await _client(handler).discover(_provider(protocol, url.removesuffix('/models')))
    assert result.models[0].id == 'arbitrary-alias'
    assert result.models[0].owned_by == 'owner' and result.models[0].created_at == '123'
    assert result.models[0].capabilities.effort_levels is None
    assert len(seen) == 1 and seen[0].method == 'GET'


@pytest.mark.asyncio
async def test_claude_root_url_and_pagination_deduplicate_ids():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.path == '/v1/models'
        if len(seen) == 1:
            assert 'after_id' not in request.url.params
            return httpx.Response(200, json={'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'})
        assert request.url.params['after_id'] == 'a'
        return httpx.Response(200, json={'data': [{'id': 'a'}, {'id': 'b'}], 'has_more': False})
    result = await _client(handler).discover(_provider('claude', 'https://gateway.test'))
    assert [model.id for model in result.models] == ['a', 'b']


@pytest.mark.asyncio
async def test_cache_is_isolated_by_connection_auth_and_protocol():
    seen = []
    client = _client(lambda request: (seen.append(request), httpx.Response(200, json={'data': [{'id': 'a'}]}))[1])
    provider = _provider()
    assert not (await client.discover(provider)).cached
    assert (await client.discover(provider)).cached
    assert not (await client.discover(provider, refresh=True)).cached
    await client.discover(replace(provider, api_key='different-key'))
    await client.discover(replace(provider, protocol='claude'))
    await client.discover(replace(provider, base_url='https://other.test/v1'))
    assert len(seen) == 5


@pytest.mark.asyncio
async def test_refresh_failure_can_show_explicitly_stale_cache_without_changing_snapshot():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ConnectError('secret-key must never be displayed', request=request)
        return httpx.Response(200, json={'data': [{'id': 'a'}]})
    client = _client(handler)
    fresh = await client.discover(_provider())
    stale = await client.discover(_provider(), refresh=True)
    assert stale.stale and stale.cached and '上次' in stale.notice
    assert stale.models == fresh.models and not fresh.stale
    assert 'secret-key' not in stale.notice


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error', [(401, ProviderAuthError), (403, ProviderAuthError),
                                         (404, ProviderResponseError), (405, ProviderResponseError), (500, ProviderResponseError)])
async def test_http_failure_is_explicit_and_never_echoes_credentials(status, error):
    client = _client(lambda request: httpx.Response(status, json={'error': 'secret-key'}))
    with pytest.raises(error) as exception:
        await client.discover(_provider())
    assert 'secret-key' not in str(exception.value)


@pytest.mark.asyncio
async def test_auth_error_does_not_fall_back_to_cached_visibility():
    statuses = iter([200, 401])
    client = _client(lambda request: httpx.Response(next(statuses), json={'data': [{'id': 'a'}]}))
    await client.discover(_provider())
    with pytest.raises(ProviderAuthError):
        await client.discover(_provider(), refresh=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{'data': 'invalid'}, [], {'data': [{'id': 'a'}], 'has_more': True},
                                     {'data': [], 'has_more': True, 'last_id': 'a'},
                                     {'data': [{'id': 'a'}], 'has_more': 'true'}])
async def test_invalid_or_incomplete_list_is_not_accepted(payload):
    with pytest.raises(ProviderResponseError):
        await _client(lambda request: httpx.Response(200, json=payload)).discover(_provider())


@pytest.mark.asyncio
async def test_repeated_pagination_cursor_cannot_loop_forever():
    calls = []
    client = _client(lambda request: (calls.append(request), httpx.Response(200, json={
        'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'}))[1])
    with pytest.raises(ProviderResponseError, match='游标'):
        await client.discover(_provider('claude'))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancelled_discovery_closes_client_and_does_not_cache_partial_result():
    started = asyncio.Event()
    release = asyncio.Event()
    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={'data': [{'id': 'a'}]})
    client = _client(handler)
    task = asyncio.create_task(client.discover(_provider()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client._cache


def test_metadata_preserves_declared_capabilities_and_unknown_values():
    raw = {'id': 'custom-alias', 'display_name': '真正的名称', 'context_window': 64000,
           'max_output_tokens': 4096, 'capabilities': {'thinking': {'types': {'adaptive': {'supported': True}}},
           'effort': {'supported': True, 'low': {'supported': True}, 'max': {'supported': False}}}}
    model = parse_model_metadata(raw, fingerprint='fingerprint', fetched_at='now')
    assert model.capabilities.thinking_types == ('adaptive',)
    assert model.capabilities.effort_levels == ('low',)
    assert model.capabilities.default_effort is None
    assert model.context_window == 64000 and model.max_output_tokens == 4096
    assert load_metadata(serialize_metadata(model), 'metadata') == model
    unknown = parse_model_metadata({'id': 'unknown'}, fingerprint='fingerprint', fetched_at='now')
    assert unknown.capabilities == ModelCapabilities()
    partial = parse_model_metadata({'id': 'partial', 'capabilities': {'thinking': {'types': {
        'adaptive': {'supported': None}}}, 'effort': {'high': {'supported': None}}}}, fingerprint='f', fetched_at='now')
    assert partial.capabilities.thinking_types is None and partial.capabilities.effort_levels is None


def test_openai_compatible_effort_extension_is_parsed_without_vendor_detection():
    model = parse_model_metadata({'id': 'unbranded', 'effort': {
        'supported_levels': ['none', 'low', 'high', 'max'], 'default_level': 'high'}}, fingerprint='f', fetched_at='now')
    assert model.capabilities.effort_levels == ('low', 'high', 'max')
    assert model.capabilities.default_effort == 'high'


@pytest.mark.parametrize('support', [True, False, None])
def test_explicit_cache_support_is_preserved_and_unknown_is_not_inferred(support):
    raw = {'id': 'unbranded', 'capabilities': {'prompt_caching': {
        'explicit_breakpoints': {'supported': support}}}}
    metadata = parse_model_metadata(raw, fingerprint='f', fetched_at='now')
    assert metadata.capabilities.prompt_cache_breakpoints is support
    assert load_metadata(serialize_metadata(metadata), 'metadata') == metadata
    snapshot = serialize_metadata(metadata)
    snapshot['capabilities']['prompt_cache_breakpoints'] = 'true'
    with pytest.raises(ConfigError, match='快照格式无效'):
        load_metadata(snapshot, 'metadata')


def test_merge_is_additive_idempotent_and_preserves_manual_settings_and_overrides():
    provider = _provider()
    provider.models = {'stable': ModelDefinition('a', display_name='我的名称', context_window=16000, max_output_tokens=2048),
                       'override': ModelDefinition('b', base_url='https://other.test/v1')}
    fingerprint = connection_fingerprint(provider.protocol, provider.base_url, provider.api_key)
    models = [parse_model_metadata({'id': name, 'name': '在线名称'}, fingerprint=fingerprint, fetched_at='now')
              for name in ('a', 'b', 'odd/name')]
    merged, added, updated = merge_discovered_models(provider, models, ['a', 'b', 'odd/name'])
    assert (added, updated) == (2, 1)
    assert merged.models['stable'].display_name == '我的名称' and merged.models['stable'].context_window == 16000
    assert merged.models['stable'].max_output_tokens == 2048
    assert merged.models['override'] == provider.models['override']
    assert len(provider.models) == 2 and provider.models['stable'].metadata is None
    merged_again, added, updated = merge_discovered_models(merged, models, ['a', 'b', 'odd/name'])
    assert (added, updated) == (0, 0) and merged_again == merged


def test_metadata_is_invalidated_by_model_connection_or_auth_changes():
    provider = _provider()
    fingerprint = connection_fingerprint(provider.protocol, provider.base_url, provider.api_key)
    metadata = parse_model_metadata({'id': 'a', 'context_window': 64000, 'max_output_tokens': 4096,
        'effort': {'supported_levels': ['low', 'high'], 'default_level': 'high'}}, fingerprint=fingerprint, fetched_at='now')
    provider.models['a'] = ModelDefinition('a', metadata=metadata)
    original = resolve_model({'p': provider}, 'p/a')
    assert original.metadata == metadata and original.max_output_tokens == 4096 and original.context_window == 64000
    provider.api_key = 'new-key'
    changed = resolve_model({'p': provider}, 'p/a')
    assert changed.metadata is None and changed.context_window == 128000
    assert original.metadata == metadata


@pytest.mark.parametrize('field,value', [('api_key', '${MISSING_DISCOVERY_KEY}'), ('base_url', 'https://['),
                                        ('api_key', 'key\nsecret'),
                                        ('base_url', 'file:///tmp'), ('base_url', 'https://a.test/v1?q=secret'),
                                        ('timeout_seconds', True)])
def test_discovery_rejects_invalid_connections_before_sending(field, value):
    with pytest.raises((ProviderAuthError, ProviderRequestError)):
        discovery_connection(replace(_provider(), **{field: value}))


@pytest.mark.asyncio
async def test_discovery_does_not_follow_redirects_with_credentials():
    seen = []
    client = _client(lambda request: (seen.append(request), httpx.Response(302, headers={
        'location': 'https://another-origin.test/models'}))[1])
    with pytest.raises(ProviderResponseError, match='HTTP 302'):
        await client.discover(_provider())
    assert len(seen) == 1 and seen[0].url.host == 'gateway.test'


@pytest.mark.asyncio
async def test_expired_list_cache_is_refreshed_automatically():
    now, seen = [0.0], []
    client = _client(lambda request: (seen.append(request), httpx.Response(200, json={'data': [{'id': 'a'}]}))[1],
                     clock=lambda: now[0], cache_ttl=10)
    await client.discover(_provider())
    now[0] = 9
    assert (await client.discover(_provider())).cached
    now[0] = 11
    assert not (await client.discover(_provider())).cached
    assert len(seen) == 2
