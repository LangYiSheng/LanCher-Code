from __future__ import annotations

import os
from copy import deepcopy
from collections.abc import Callable
from lancher_code.config.models import AppConfig
from lancher_code.errors import ConfigError
from lancher_code.logging_system import register_sensitive_values
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.factory import create_provider
from lancher_code.providers.models import ProviderConfig
from lancher_code.providers.catalog import resolve_model, iter_model_refs, model_display_name
from lancher_code.sessions.controller import SessionController


class ModelSelection:
    """准备模型切换；先提交会话状态，再替换实际请求供应商。"""

    def __init__(self, provider: ChatProvider, session: SessionController):
        self.provider = provider
        self._session = session
        self._model_config: AppConfig | None = None
        self._provider_factory: Callable[[ProviderConfig], ChatProvider] = create_provider
        self._model_notice = ""

    def configure_models(
        self,
        config: AppConfig,
        provider_factory: Callable[[ProviderConfig], ChatProvider] = create_provider,
    ) -> None:
        """绑定启动时的模型目录，复用应用已经创建的 provider。"""
        snapshot = deepcopy(config)
        resolved = resolve_model(snapshot.providers, snapshot.default_model)
        self._register_model_secrets(snapshot)
        self._session.set_model(resolved, snapshot.default_model, initial=True)
        self._model_config = snapshot
        self._provider_factory = provider_factory
        self._model_notice = ""

    @property
    def model_config(self) -> AppConfig | None:
        return self._model_config

    def _require_model_config(self) -> AppConfig:
        if self._model_config is None:
            raise ConfigError("尚未配置模型目录。")
        return self._model_config

    @staticmethod
    def _register_model_secrets(config: AppConfig) -> None:
        values: list[str] = []
        for provider in config.providers.values():
            candidates = [provider.api_key, *(model.api_key for model in provider.models.values())]
            for candidate in candidates:
                if isinstance(candidate, str):
                    values.extend((candidate, os.path.expandvars(candidate).strip()))
        register_sensitive_values(values)

    @property
    def model_ref(self) -> str | None:
        return self._session.selected_model_ref

    @property
    def model_notice(self) -> str:
        return self._model_notice

    def _prepare_model(self, config: AppConfig, model_ref: str) -> tuple[ProviderConfig, ChatProvider]:
        resolved = resolve_model(config.providers, model_ref)
        # 工厂创建失败时也可能记录错误，必须提前注册已经解析的密钥。
        register_sensitive_values([resolved.api_key])
        return resolved, self._provider_factory(resolved)

    def switch_model(self, model_ref: str) -> None:
        resolved, provider = self._prepare_model(self._require_model_config(), model_ref)
        self._session.set_model(resolved, model_ref)
        self.provider = provider
        self._model_notice = ""

    def reload_models(self, config: AppConfig) -> bool:
        """热更新目录；修改默认值不会覆盖会话中已经选择的模型。"""
        snapshot = deepcopy(config)
        self._register_model_secrets(snapshot)
        resolve_model(snapshot.providers, snapshot.default_model)
        fallback = self.model_ref not in iter_model_refs(snapshot.providers)
        target = snapshot.default_model if fallback else self.model_ref
        assert target is not None
        resolved = resolve_model(snapshot.providers, target)
        if target != self.model_ref or resolved != self._session.provider_config:
            resolved, provider = self._prepare_model(snapshot, target)
            self._session.set_model(resolved, target)
            self.provider = provider
        self._model_config = snapshot
        self._model_notice = (
            f"当前模型已被删除，已切换到默认模型：{model_display_name(snapshot.providers, target)}。"
            if fallback else ""
        )
        return fallback

    def resume_session(self, session_id: str) -> int:
        if self._model_config is None:
            self._model_notice = ''
            return self._session.resume_session(session_id)
        saved_ref = self._session.read_session_model_ref(session_id)
        config = self._require_model_config()
        target = saved_ref if saved_ref in iter_model_refs(config.providers) else config.default_model
        resolved, provider = self._prepare_model(config, target)
        permission_count = self._session.resume_session(
            session_id, resolved_model=(resolved, target)
        )
        self.provider = provider
        if saved_ref is None:
            self._model_notice = f"会话未选择模型，已使用默认模型：{model_display_name(config.providers, target)}。"
        elif saved_ref != target:
            self._model_notice = f"会话原模型已不存在，已使用默认模型：{model_display_name(config.providers, target)}。"
        else:
            self._model_notice = ""
        return permission_count

    def new_session(self) -> None:
        if self._model_config is not None:
            target = self._model_config.default_model
            resolved, provider = self._prepare_model(self._model_config, target)
            self._session.new_session()
            self._session.set_model(resolved, target, initial=True)
            self.provider = provider
        else:
            self._session.new_session()
        self._model_notice = ''
