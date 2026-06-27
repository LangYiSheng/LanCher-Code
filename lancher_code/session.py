from __future__ import annotations

from lancher_code.models import ChatRequest, ProviderConfig, SessionState


class SessionController:
    """管理当前进程内的会话状态。"""

    def __init__(self, provider_config: ProviderConfig, state: SessionState | None = None) -> None:
        self._provider_config = provider_config
        self._state = state or SessionState()

    @property
    def state(self) -> SessionState:
        return self._state

    def record_user_message(self, text: str) -> None:
        self._state.add_message("user", text)

    def record_assistant_message(self, text: str) -> None:
        self._state.add_message("assistant", text)

    def build_request(self) -> ChatRequest:
        thinking = None
        if self._provider_config.protocol == "claude":
            thinking = self._provider_config.thinking

        return ChatRequest(
            model=self._provider_config.model,
            messages=self._state.snapshot(),
            thinking=thinking,
        )
