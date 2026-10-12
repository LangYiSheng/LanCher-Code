"""仅使用连接绑定的能力声明和用户选择，不按型号或域名猜测推理能力。"""
from __future__ import annotations

from dataclasses import dataclass

from lancher_code.errors import ConfigError
from lancher_code.providers.metadata import API_MODES, POSITIVE_EFFORTS, ModelMetadata

EFFORT_LEVELS = ('auto', *POSITIVE_EFFORTS)
EFFORT_LABELS = {'auto': '自动（推荐）', 'low': '低 · 更快', 'medium': '中 · 均衡',
                 'high': '高 · 深入', 'xhigh': '很高', 'max': '最高'}
API_MODE_OPTIONS = [('自动', 'auto'), ('标准聊天', 'chat_completions'), ('兼容聊天', 'chat_completions_legacy'), ('Responses', 'responses')]


@dataclass(frozen=True, slots=True)
class ReasoningCapabilities:
    mode: str = 'default'
    levels: tuple[str, ...] | None = None
    default_effort: str | None = None

    @property
    def description(self) -> str:
        if self.levels is None:
            return '端点未声明推理档位；自动使用服务端默认，手动档位由端点校验。'
        if self.mode == 'legacy':
            return '端点声明支持旧版思考接口，应用自动分配额度；effort 取自模型信息。'
        if self.mode == 'adaptive':
            return '端点声明支持自适应推理，由模型决定思考深度；档位取自模型信息。'
        return '推理档位取自端点返回的模型信息；思考类型沿用服务端默认。'


def reasoning_capabilities(protocol: str, metadata: ModelMetadata | None) -> ReasoningCapabilities:
    cap = metadata.capabilities if metadata is not None else None
    mode = 'default'
    if protocol == 'claude' and cap is not None and cap.thinking_types is not None:
        if 'adaptive' in cap.thinking_types:
            mode = 'adaptive'
        elif 'enabled' in cap.thinking_types:
            mode = 'legacy'
    return ReasoningCapabilities(mode, cap.effort_levels if cap else None, cap.default_effort if cap else None)


def validate_effort(protocol: str, metadata: ModelMetadata | None, effort: str, *, path: str = 'reasoning_effort') -> ReasoningCapabilities:
    capabilities = reasoning_capabilities(protocol, metadata)
    allowed = capabilities.levels if capabilities.levels is not None else POSITIVE_EFFORTS
    if not isinstance(effort, str) or effort in {'none', 'off', 'disabled'} or effort not in ('auto', *allowed):
        choices = '、'.join(('auto', *allowed))
        raise ConfigError(f'{path}：可选推理档位为 {choices}。')
    return capabilities


def resolve_api_mode(protocol: str, metadata: ModelMetadata | None, mode: str) -> str:
    if not isinstance(mode, str) or mode not in API_MODES:
        raise ConfigError('api_mode 必须是 auto、chat_completions、chat_completions_legacy 或 responses。')
    if protocol != 'openai':
        return 'auto'
    endpoints = metadata.capabilities.endpoints if metadata else None
    if mode != 'auto':
        endpoint = 'chat_completions' if mode == 'chat_completions_legacy' else mode
        if endpoints is not None and endpoint not in endpoints and mode not in endpoints:
            raise ConfigError('端点的模型信息未声明支持所选 OpenAI 接口形式。')
        return mode
    if endpoints is None:
        return 'chat_completions_legacy'
    if 'chat_completions' in endpoints:
        return 'chat_completions'
    if 'responses' in endpoints:
        return 'responses'
    if 'chat_completions_legacy' in endpoints:
        return 'chat_completions_legacy'
    raise ConfigError('端点声明此模型没有当前应用支持的 OpenAI 对话接口。')


def reasoning_parameters(*, protocol: str, metadata: ModelMetadata | None, effort: str,
                         max_tokens: int, responses: bool = False) -> dict[str, object]:
    cap = validate_effort(protocol, metadata, effort)
    selected = cap.default_effort if effort == 'auto' else effort
    params: dict[str, object] = {}
    if cap.mode == 'adaptive':
        params['thinking'] = {'type': 'adaptive'}
    elif cap.mode == 'legacy':
        budget = min(8192, max_tokens // 2)
        if budget < 1024:
            raise ConfigError('此模型的旧版思考接口至少需要 2048 token 总输出额度。')
        params['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
    if selected is not None:
        if protocol == 'claude':
            params['output_config'] = {'effort': selected}
        elif responses:
            params['reasoning'] = {'effort': selected}
        else:
            params['reasoning_effort'] = selected
    return params
