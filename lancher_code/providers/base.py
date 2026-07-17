from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Protocol

import httpx

from lancher_code.errors import (
    ProviderAuthError,
    ProviderPromptTooLongError,
    ProviderRequestError,
    ProviderResponseError,
    StreamProtocolError,
)
from lancher_code.models import ChatRequest, ContentBlock, ConversationMessage, MessageUsage, ProviderConfig, StreamEvent


class ChatProvider(Protocol):
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """以统一流事件输出模型回复。"""


class BaseChatProvider:
    def __init__(
        self,
        config: ProviderConfig,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or self._default_client_factory

    def _default_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.config.timeout_seconds)

    @staticmethod
    def build_usage(
        raw_usage: object,
        *,
        input_keys: tuple[str, ...],
        output_keys: tuple[str, ...],
        cached_input_keys: tuple[str, ...] = (),
    ) -> MessageUsage:
        if not isinstance(raw_usage, dict):
            return MessageUsage()

        input_tokens = BaseChatProvider._read_usage_value(raw_usage, input_keys)
        output_tokens = BaseChatProvider._read_usage_value(raw_usage, output_keys)
        cached_input_tokens = BaseChatProvider._read_usage_value(raw_usage, cached_input_keys)
        return MessageUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )

    @staticmethod
    async def iter_sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
        event_name = "message"
        data_lines: list[str] = []

        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name = "message"
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            yield event_name, "\n".join(data_lines)

    @staticmethod
    def parse_json_payload(data: str) -> dict:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise StreamProtocolError("流式响应不是合法 JSON。") from exc
        if not isinstance(payload, dict):
            raise StreamProtocolError("流式响应格式不正确。")
        return payload

    @staticmethod
    async def raise_for_error_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return

        message = await BaseChatProvider.extract_error_message(response)
        code = await BaseChatProvider.extract_error_code(response)
        if response.status_code in (401, 403):
            raise ProviderAuthError(message or "模型供应商认证失败，请检查 API Key。")
        if BaseChatProvider.is_prompt_too_long(message, code=code):
            raise ProviderPromptTooLongError(message or "请求超过模型上下文窗口。")
        raise ProviderResponseError(
            message or f"模型供应商返回错误状态码 {response.status_code}。"
        )

    @staticmethod
    async def extract_error_message(response: httpx.Response) -> str:
        body = await response.aread()
        if not body:
            return ""
        text = body.decode("utf-8", errors="ignore").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return text

    @staticmethod
    async def extract_error_code(response: httpx.Response) -> str | None:
        body = await response.aread()
        try:
            payload = json.loads(body.decode("utf-8", errors="ignore"))
        except (json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            value = error.get("code") or error.get("type")
            return value if isinstance(value, str) else None
        value = payload.get("code") or payload.get("type")
        return value if isinstance(value, str) else None

    @staticmethod
    def map_request_error(exc: Exception) -> ProviderRequestError:
        if isinstance(exc, httpx.TimeoutException):
            return ProviderRequestError("请求模型超时，请稍后重试。")
        if isinstance(exc, httpx.RequestError):
            return ProviderRequestError(f"请求模型失败: {exc}")
        return ProviderRequestError("请求模型失败。")

    @staticmethod
    def is_prompt_too_long(message: str, *, code: str | None = None) -> bool:
        normalized_code = (code or "").strip().casefold()
        if normalized_code in {
            "context_length_exceeded",
            "prompt_too_long",
            "request_too_large",
            "context_window_exceeded",
        }:
            return True
        normalized = message.casefold()
        return any(
            marker in normalized
            for marker in (
                "maximum context length",
                "context length exceeded",
                "prompt is too long",
                "prompt too long",
                "exceeds the context window",
            )
        )

    @staticmethod
    def text_from_blocks(blocks: list[ContentBlock]) -> str:
        return "".join(block.text for block in blocks if block.kind == "text")

    @staticmethod
    def split_system_and_chat_messages(
        messages: list[ConversationMessage],
    ) -> tuple[list[ConversationMessage], list[ConversationMessage]]:
        system_messages: list[ConversationMessage] = []
        chat_messages: list[ConversationMessage] = []
        for message in messages:
            if message.role == "system":
                system_messages.append(message)
            else:
                chat_messages.append(message)
        return system_messages, chat_messages

    @staticmethod
    def _read_usage_value(raw_usage: dict[str, object], keys: tuple[str, ...]) -> int:
        for key in keys:
            value = BaseChatProvider._read_nested_usage_value(raw_usage, key)
            if isinstance(value, int):
                return value
        return 0

    @staticmethod
    def _read_nested_usage_value(raw_usage: dict[str, object], key: str) -> object:
        current: object = raw_usage
        for part in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current
