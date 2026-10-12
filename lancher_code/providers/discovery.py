"""OpenAI / Claude 模型列表发现、分页与按连接隔离的缓存。"""
from __future__ import annotations

import math
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import httpx

from lancher_code.errors import ProviderAuthError, ProviderRequestError, ProviderResponseError
from lancher_code.providers.metadata import ModelMetadata, connection_fingerprint, parse_model_metadata
from lancher_code.providers.models import ProviderDefinition


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    models: tuple[ModelMetadata, ...]
    fetched_at: str
    fingerprint: str
    cached: bool = False
    stale: bool = False
    notice: str = ''


def discovery_connection(provider: ProviderDefinition) -> tuple[str, str, str, float]:
    protocol = provider.protocol
    base_url = os.path.expandvars(provider.base_url).strip().rstrip('/')
    api_key = os.path.expandvars(provider.api_key).strip()
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise ProviderRequestError('请填写有效的 API 地址后再发现模型。') from exc
    if protocol not in {'openai', 'claude'} or parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        raise ProviderRequestError('模型发现需要有效的 HTTP(S) API 基址，不应包含查询参数或账号。')
    if not api_key or '${' in api_key:
        raise ProviderAuthError('请填写 API Key，或设置其引用的环境变量后再发现模型。')
    if any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        raise ProviderAuthError('API Key 含有非法控制字符，请重新填写。')
    timeout = provider.timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ProviderRequestError('模型发现的超时必须为有限正数。')
    return protocol, base_url, api_key, min(float(timeout), 20.0)


def models_url(protocol: str, base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip('/')
    # Claude 官方协议的根地址使用 /v1；有路径的兼容基址保留其挂载路径。
    if protocol == 'claude' and not path:
        path = '/v1'
    return urlunsplit((parsed.scheme, parsed.netloc, path + '/models', '', ''))


class ModelDiscoveryClient:
    def __init__(self, client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
                 *, cache_ttl: float = 86400, clock: Callable[[], float] = monotonic) -> None:
        self._client_factory, self._cache_ttl, self._clock = client_factory, cache_ttl, clock
        self._cache: dict[str, tuple[float, ModelDiscoveryResult]] = {}

    async def discover(self, provider: ProviderDefinition, *, refresh: bool = False) -> ModelDiscoveryResult:
        protocol, base_url, api_key, timeout = discovery_connection(provider)
        fingerprint = connection_fingerprint(protocol, base_url, api_key)
        cached = self._cache.get(fingerprint)
        if not refresh and cached and self._clock() - cached[0] < self._cache_ttl:
            return replace(cached[1], cached=True)
        try:
            result = await self._fetch(protocol, base_url, api_key, timeout, fingerprint)
        except (ProviderRequestError, ProviderResponseError) as exc:
            if cached:
                return replace(cached[1], cached=True, stale=True,
                               notice=f'{exc.user_message} 当前显示上次成功发现的模型，请核对后加入。')
            raise
        self._cache[fingerprint] = (self._clock(), result)
        return result

    async def _fetch(self, protocol: str, base_url: str, api_key: str, timeout: float,
                     fingerprint: str) -> ModelDiscoveryResult:
        headers = {'Authorization': f'Bearer {api_key}'} if protocol == 'openai' else {
            'x-api-key': api_key, 'anthropic-version': '2023-06-01'}
        url = models_url(protocol, base_url)
        fetched_at = datetime.now(timezone.utc).isoformat()
        models: dict[str, ModelMetadata] = {}
        cursors: set[str] = set()
        cursor: str | None = None
        skipped = 0
        try:
            async with self._client_factory(timeout=timeout, follow_redirects=False) as client:
                for _ in range(100):
                    params = {'after_id': cursor} if cursor else None
                    async with client.stream('GET', url, headers=headers, params=params) as response:
                        if response.status_code in {401, 403}:
                            raise ProviderAuthError('模型列表认证失败，请检查 API Key 及访问权限。')
                        if response.status_code in {404, 405}:
                            raise ProviderResponseError('此连接未提供兼容的模型列表接口，可以继续手动添加模型。')
                        if response.status_code != 200:
                            raise ProviderResponseError(f'读取模型列表失败（HTTP {response.status_code}），请稍后刷新。')
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > 4 * 1024 * 1024:
                                raise ProviderResponseError('模型列表响应过大，未采用不完整的结果。')
                            body.extend(chunk)
                        try:
                            raw = json.loads(body)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise ProviderResponseError('模型列表不是有效的 JSON，未修改已有模型。') from exc
                    if not isinstance(raw, dict) or not isinstance(raw.get('data'), list):
                        raise ProviderResponseError('模型列表缺少 data 数组，未修改已有模型。')
                    for item in raw['data']:
                        try:
                            if not isinstance(item, dict):
                                raise ValueError('无效模型条目')
                            model = parse_model_metadata(item, fingerprint=fingerprint, fetched_at=fetched_at)
                        except ValueError:
                            skipped += 1
                            continue
                        models.setdefault(model.id, model)
                        if len(models) > 10000:
                            raise ProviderResponseError('模型数量超过发现上限，未采用不完整的列表。')
                    if 'has_more' in raw and type(raw['has_more']) is not bool:
                        raise ProviderResponseError('模型列表分页标志无效，未采用不完整的结果。')
                    if raw.get('has_more') is not True:
                        break
                    next_cursor = raw.get('last_id')
                    if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors or not raw['data']:
                        raise ProviderResponseError('模型列表分页游标无效，未采用不完整的结果。')
                    cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise ProviderResponseError('模型列表分页次数过多，未采用不完整的结果。')
        except (httpx.HTTPError, OSError, UnicodeError, ValueError) as exc:
            # 远端错误和异常中的 URL 可能含敏感内容，只显示固定的连接提示。
            raise ProviderRequestError('读取模型列表时连接失败或超时，请检查地址后刷新。') from exc
        notice = f'忽略了 {skipped} 条缺少有效模型身份的记录。' if skipped else ''
        return ModelDiscoveryResult(tuple(models.values()), fetched_at, fingerprint, notice=notice)
