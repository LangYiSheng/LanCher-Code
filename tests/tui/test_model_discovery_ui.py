"""模型发现的实际界面流程，模块名与协议测试分开。"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from textual.widgets import Button, Input, Select, SelectionList, Static

from lancher_code.config.loader import load_config
from lancher_code.providers.discovery import ModelDiscoveryClient
from lancher_code.providers.catalog import resolve_model
from lancher_code.tui.bootstrap import ConfigBootstrapApp
from lancher_code.tui.model_discovery import ModelDiscoveryScreen
from lancher_code.tui.settings.models import ModelSettingsEditor
from tests.tui.test_settings import _service, _settings_app


def _payload():
    return {'data': [{'id': 'gpt-test', 'owned_by': 'first-owner'},
        {'id': 'alias/a', 'display_name': '[bold]真实模型[/bold]', 'owned_by': 'second-owner',
         'context_window': 64000, 'max_output_tokens': 4096,
         'effort': {'supported_levels': ['low', 'high', 'max'], 'default_level': 'high'}},
        {'id': 'alias-b', 'owned_by': 'second-owner'}]}


def _client(handler):
    return ModelDiscoveryClient(client_factory=lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), **kwargs))


async def _ready(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [(100, 40), (32, 16)])
async def test_settings_multiselect_filter_and_import_preserve_default_and_existing_model(tmp_path, size):
    seen = []
    service = _service(tmp_path)
    service.model_discovery = _client(lambda request: (seen.append(request), httpx.Response(200, json=_payload()))[1])
    app = _settings_app(service)
    async with app.run_test(size=size) as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_provider('test')
        screen.discovery_requested()
        await _ready(app, pilot)
        picker = app.screen
        assert isinstance(picker, ModelDiscoveryScreen)
        assert await pilot.click('#model-discovery-all')
        await pilot.pause()
        picker.query_one('#model-discovery-search', Input).value = 'alias-b'
        await pilot.pause()
        assert len(picker._selected) == 2
        assert picker.query_one('#model-discovery-options', SelectionList).option_count == 1
        assert await pilot.click('#model-discovery-add')
        await pilot.pause()
        saved = service.load().config
        assert saved.default_model == screen.current_model_ref == 'test/default'
        assert set(model.model_name for model in saved.providers['test'].models.values()) == {'gpt-test', 'alias/a', 'alias-b'}
        assert saved.providers['test'].models['default'].model_name == 'gpt-test'
        added = next(model for model in saved.providers['test'].models.values() if model.model_name == 'alias/a')
        assert added.display_name == '[bold]真实模型[/bold]'
        assert added.metadata.owned_by == 'second-owner'
        ref = next(key for key, model in saved.providers['test'].models.items() if model.model_name == 'alias/a')
        resolved = resolve_model(saved.providers, f'test/{ref}')
        assert resolved.context_window == 64000 and resolved.max_output_tokens == 4096
        editor.open_provider('test')
        screen.discovery_requested()
        await _ready(app, pilot)
        picker = app.screen
        assert picker._existing == {'gpt-test', 'alias/a', 'alias-b'}
        picker.select_all()
        assert not picker._selected
        picker.add_selected()
        await pilot.pause()
        assert len(service.load().config.providers['test'].models) == 3
        assert len(seen) == 2 and all(request.method == 'GET' for request in seen)


@pytest.mark.asyncio
async def test_new_provider_save_automatically_discovers_and_cancel_does_not_import(tmp_path):
    service = _service(tmp_path)
    seen = []
    service.model_discovery = _client(lambda request: (seen.append(request), httpx.Response(200, json=_payload()))[1])
    app = _settings_app(service)
    async with app.run_test() as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_provider()
        screen.query_one('#provider-name', Input).value = '新供应商'
        screen.query_one('#provider-base-url', Input).value = 'https://custom.test/v1'
        screen.query_one('#provider-api-key', Input).value = 'new-key'
        await pilot.pause()
        screen.action_save_settings()
        await _ready(app, pilot)
        assert isinstance(app.screen, ModelDiscoveryScreen)
        saved = service.load().config
        provider_id = next(key for key in saved.providers if key != 'test')
        assert not saved.providers[provider_id].models
        app.screen.action_cancel()
        await pilot.pause()
        assert not service.load().config.providers[provider_id].models
        assert service.load().config.default_model == 'test/default'
        assert len(seen) == 1


@pytest.mark.asyncio
async def test_discover_unsaved_provider_and_bulk_add_is_one_reviewed_commit(tmp_path):
    service = _service(tmp_path)
    service.model_discovery = _client(lambda request: httpx.Response(200, json=_payload()))
    app = _settings_app(service)
    async with app.run_test() as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_provider()
        screen.query_one('#provider-name', Input).value = '草稿供应商'
        screen.query_one('#provider-api-key', Input).value = 'draft-key'
        await pilot.pause()
        screen.discovery_requested()
        await _ready(app, pilot)
        assert len(service.load().config.providers) == 1
        app.screen.select_all()
        app.screen.add_selected()
        await pilot.pause()
        saved = service.load().config
        provider = next(provider for key, provider in saved.providers.items() if key != 'test')
        assert len(provider.models) == 3 and provider.api_key == 'draft-key'
        assert saved.default_model == 'test/default'


@pytest.mark.asyncio
async def test_changed_connection_discards_late_response_and_preserves_draft(tmp_path):
    service = _service(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=_payload())
    service.model_discovery = _client(handler)
    app = _settings_app(service)
    async with app.run_test() as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_provider('test')
        screen.discovery_requested()
        await started.wait()
        screen.query_one('#provider-base-url', Input).value = 'https://changed.test/v1'
        release.set()
        await _ready(app, pilot)
        assert app.screen is screen
        assert screen.query_one('#provider-base-url', Input).value == 'https://changed.test/v1'
        assert len(service.load().config.providers['test'].models) == 1


@pytest.mark.asyncio
async def test_discovery_error_is_visible_and_manual_model_entry_remains_available(tmp_path):
    service = _service(tmp_path)
    service.model_discovery = _client(lambda request: httpx.Response(404, json={'error': 'secret'}))
    app = _settings_app(service)
    async with app.run_test() as pilot:
        screen = app.screen
        editor = screen.query_one(ModelSettingsEditor)
        editor.open_provider('test')
        screen.discovery_requested()
        await _ready(app, pilot)
        assert screen.query_one('#settings-error', Static).display
        assert '手动' in str(screen.query_one('#settings-error', Static).render())
        assert not screen.query_one('#provider-discover', Button).disabled
        editor.open_model('test')
        assert editor.kind == 'model'


@pytest.mark.asyncio
async def test_bootstrap_automatically_discovers_and_persists_all_selected_models(tmp_path):
    path = tmp_path / 'home' / '.lancher' / 'lancher.yaml'
    seen = []
    app = ConfigBootstrapApp(path, model_discovery=_client(lambda request: (seen.append(request),
        httpx.Response(200, json=_payload()))[1]))
    async with app.run_test(size=(100, 40)) as pilot:
        app.query_one('#provider-name-input', Input).value = '任意供应商'
        app.query_one('#base-url-input', Input).value = 'https://gateway.test/v1'
        app.query_one('#api-key-input', Input).value = 'test-key'
        await pilot.pause()
        app.action_continue()
        await _ready(app, pilot)
        assert isinstance(app.screen, ModelDiscoveryScreen)
        app.screen.select_all()
        app.screen.add_selected()
        await pilot.pause()
        assert app.query_one('#model-input', Input).value == 'gpt-test'
        app.action_continue()
        assert app._step == 2
        assert '3 个模型' in str(app.query_one('#bootstrap-summary', Static).render())
        app._save()
        await pilot.pause()
    saved = load_config(path)
    provider = next(iter(saved.providers.values()))
    assert len(provider.models) == 3
    assert resolve_model(saved.providers, saved.default_model).model == 'gpt-test'
    assert len(seen) == 1
