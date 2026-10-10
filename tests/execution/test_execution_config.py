from __future__ import annotations

import pytest

from lancher_code.config import load_config_data, serialize_config
from lancher_code.errors import ConfigError
from lancher_code.execution.runtime import ExecutionRuntime


def config_data(execution):
    return {'provider': {'protocol': 'openai', 'model': 'test',
                         'base_url': 'https://example.test/v1', 'api_key': 'test'},
            'execution': execution}


def test_execution_config_round_trip_and_resource_normalization(tmp_path):
    raw = {'limits': {'max_concurrency': 4}, 'command_profiles': [
        {'name': '服务', 'command_match': 'npm run dev', 'resources': [
            {'kind': 'path', 'key': '.cache', 'recursive': True},
            {'kind': 'external', 'key': 'port:5173'},
            {'kind': 'project', 'key': '.', 'lifetime': 'invocation'}],
         'readiness': {'port': 5173}}]}
    config = load_config_data(config_data(raw))
    restored = load_config_data(serialize_config(config))
    assert restored.execution == config.execution
    runtime = ExecutionRuntime(tmp_path, restored.execution)
    claims = runtime.command_claims('npm run dev', tmp_path)
    assert claims[0].key.endswith('/.cache')
    assert claims[0].recursive
    assert claims[1].key == 'port:5173'
    assert claims[0].lifetime == claims[1].lifetime == 'process'
    assert claims[2].lifetime == 'invocation' and claims[2].mode == 'exclusive'
    assert runtime.command_readiness('npm run dev').port == 5173
    fallback, = runtime.command_claims('npm run dev; arbitrary-write', tmp_path)
    assert fallback.kind == 'project' and fallback.lifetime == 'invocation'


@pytest.mark.parametrize('kind', ['path', 'project', 'external', 'process'])
def test_configured_claim_lifetime_survives_normalization(tmp_path, kind):
    raw = {'command_profiles': [{'name': '明确声明', 'command_match': 'test-command', 'resources': [
        {'kind': kind, 'key': 'resource', 'mode': 'shared', 'lifetime': 'invocation'}]}]}
    config = load_config_data(config_data(raw))
    runtime = ExecutionRuntime(tmp_path, load_config_data(serialize_config(config)).execution)
    claim, = runtime.command_claims('test-command', tmp_path)
    assert claim.kind == kind and claim.mode == 'shared' and claim.lifetime == 'invocation'


@pytest.mark.parametrize('execution', [
    {'limits': {'max_concurrency': True}},
    {'limits': {'max_processes': 2, 'max_processes_per_session': 3}},
    {'limits': {'output_limit_bytes': 0}},
    {'limits': {'stop_grace_seconds': float('nan')}},
    {'limits': {'typo': 4}},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [], 'readiness': {'port': 65536}}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [], 'readiness': {'port': 80, 'host': 'example.com'}}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [{'kind': 'external', 'key': 'a', 'recursive': True}]}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resouces': []}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*'}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [{'kind': 'path', 'key': 'a', 'lifetime': 'session'}]}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [{'kind': 'path', 'key': 'a', 'lifetime': None}]}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [{'kind': 'path', 'key': 'a', 'lifetime': []}]}]},
    {'command_profiles': [{'name': 'a', 'command_match': '*', 'resources': [{'kind': 'path', 'key': 'a', 'lifetime': {}}]}]},
])
def test_execution_config_rejects_invalid_limits_and_probes(execution):
    with pytest.raises(ConfigError):
        load_config_data(config_data(execution))
