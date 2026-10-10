from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

import httpx

from lancher_code.errors import (
    ProviderAuthError,
    ProviderPromptTooLongError,
    ProviderRequestError,
    ProviderResponseError,
    StreamProtocolError,
)
from lancher_code.models import ChatRequest, ContentBlock, ConversationMessage, MessageUsage, ProviderConfig, StreamEvent, merge_usage
from lancher_code.run_usage import RequestUsageRecord, RequestUsageStatus, UsageField, UsageObserver


class ChatProvider(Protocol):
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        """以统一流事件输出模型回复。"""


class BaseChatProvider:
    def __init__(
        self,
        config: ProviderConfig,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        *,
        usage_observer: UsageObserver | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or self._default_client_factory
        self._usage_observer = usage_observer
        self._usage_run_id = uuid4().hex
        self._usage_requests: dict[str, tuple[ChatRequest, RequestUsageRecord]] = {}

    def _prepare_usage_attempt(self, request: ChatRequest) -> ChatRequest:
        """每次执行使用独立副本；只消费上层为本次尝试准备的一次性 ID。"""
        prepared = request._prepared_usage_attempt_id
        request._prepared_usage_attempt_id = None
        identity = prepared if prepared is not None and prepared == request.request_id else uuid4().hex
        run_id = getattr(self._usage_observer, "run_id", None) or request.run_id or self._usage_run_id
        attempt = replace(request, request_id=identity, run_id=run_id)
        # 原对象只保留最近启动 ID；并发请求的完整归属以独立账本记录为准。
        request.request_id, request.run_id = identity, run_id
        return attempt

    def _start_usage_request(self, request: ChatRequest) -> str:
        request_id = request.request_id or uuid4().hex
        if self._usage_observer is not None:
            request_id = self._usage_observer.start_request(
                protocol=self.config.protocol, model=request.model, request_id=request_id,
                session_id=request.session_id, turn_id=request.turn_id, message_id=request.message_id,
                purpose=request.purpose)
        request.request_id = request_id
        run_id = getattr(self._usage_observer, "run_id", None) or request.run_id or self._usage_run_id
        request.run_id = run_id
        self._usage_requests[request_id] = (request, RequestUsageRecord(
            request_id=request_id, run_id=run_id, protocol=self.config.protocol, model=request.model,
            session_id=request.session_id, turn_id=request.turn_id, message_id=request.message_id,
            purpose=request.purpose))
        self._emit_usage_record(request_id)
        return request_id

    def _report_usage(
        self, request_id: str | None, usage: MessageUsage, provided_fields: frozenset[UsageField]
    ) -> None:
        if request_id is None:
            return
        pair = self._usage_requests.get(request_id)
        if pair is not None:
            pair[1].usage = merge_usage(pair[1].usage, usage)
        if self._usage_observer is not None:
            self._usage_observer.update_request(request_id, usage, provided_fields=provided_fields)
        self._emit_usage_record(request_id)

    def _finish_usage_request(self, request_id: str | None, status: RequestUsageStatus) -> None:
        if request_id is None:
            return
        pair = self._usage_requests.get(request_id)
        if pair is not None:
            pair[1].status = status
        if self._usage_observer is not None:
            self._usage_observer.finish_request(request_id, status=status)
        try:
            self._emit_usage_record(request_id)
        finally:
            self._usage_requests.pop(request_id, None)

    def _emit_usage_record(self, request_id: str) -> None:
        pair = self._usage_requests.get(request_id)
        if pair is not None and pair[0].usage_callback is not None:
            pair[0].usage_callback(pair[1].to_dict())

    @staticmethod
    def usage_fields(
        raw_usage: dict[str, object],
        *,
        input_keys: tuple[str, ...],
        output_keys: tuple[str, ...],
        cached_input_keys: tuple[str, ...] = (),
        cache_creation_keys: tuple[str, ...] = (),
        reasoning_keys: tuple[str, ...] = (),
    ) -> frozenset[UsageField]:
        fields: set[UsageField] = set()
        for field_name, keys in (("input", input_keys), ("output", output_keys), ("cache", cached_input_keys),
                                 ("cache_creation", cache_creation_keys), ("reasoning", reasoning_keys)):
            if BaseChatProvider._read_optional_usage_value(raw_usage, keys) is not None:
                fields.add(field_name)
        return frozenset(fields)

    @staticmethod
    def merge_reported_usage(
        current: MessageUsage, incoming: MessageUsage, fields: frozenset[UsageField]
    ) -> MessageUsage:
        # 缺字段不会抹掉之前的快照；服务端明确上报的零仍然覆盖旧值。
        return merge_usage(current, incoming)

    def _default_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.config.timeout_seconds)

    @staticmethod
    def build_usage(
        raw_usage: object,
        *,
        input_keys: tuple[str, ...],
        output_keys: tuple[str, ...],
        cached_input_keys: tuple[str, ...] = (),
        cache_creation_keys: tuple[str, ...] = (),
        reasoning_keys: tuple[str, ...] = (),
    ) -> MessageUsage:
        if not isinstance(raw_usage, dict):
            return MessageUsage(is_final=False)

        input_tokens = BaseChatProvider._read_optional_usage_value(raw_usage, input_keys)
        output_tokens = BaseChatProvider._read_optional_usage_value(raw_usage, output_keys)
        cached_input_tokens = BaseChatProvider._read_optional_usage_value(raw_usage, cached_input_keys)
        return MessageUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=BaseChatProvider._read_optional_usage_value(raw_usage, cache_creation_keys),
            reasoning_output_tokens=BaseChatProvider._read_optional_usage_value(raw_usage, reasoning_keys),
            is_final=False,
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
        return BaseChatProvider._read_optional_usage_value(raw_usage, keys) or 0

    @staticmethod
    def _read_optional_usage_value(raw_usage: dict[str, object], keys: tuple[str, ...]) -> int | None:
        for key in keys:
            value = BaseChatProvider._read_nested_usage_value(raw_usage, key)
            if type(value) is int and value >= 0:
                return value
        return None

    @staticmethod
    def _read_nested_usage_value(raw_usage: dict[str, object], key: str) -> object:
        current: object = raw_usage
        for part in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current
