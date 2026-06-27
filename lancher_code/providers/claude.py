from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx

from lancher_code.errors import ProviderRequestError, ProviderResponseError
from lancher_code.models import ApiMessage, ChatRequest, MessageUsage, ProviderConfig, StreamEvent
from lancher_code.providers.base import BaseChatProvider

DEFAULT_MAX_TOKENS = 4096
DEFAULT_THINKING_BUDGET = 2048


class ClaudeProvider(BaseChatProvider):
    def __init__(
        self,
        config: ProviderConfig,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__(config=config, client_factory=client_factory)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        url = f"{self.config.base_url.rstrip('/')}/messages"
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = self._build_payload(request)

        saw_end = False
        usage = MessageUsage()
        try:
            async with self._client_factory() as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await self.raise_for_error_status(response)

                    async for event_name, data in self.iter_sse_events(response):
                        if event_name == "ping":
                            continue

                        event = self.parse_json_payload(data)
                        event_type = event.get("type")
                        if event_type == "message_start":
                            message = event.get("message")
                            if isinstance(message, dict):
                                usage = self._merge_usage(usage, message.get("usage"))
                            yield StreamEvent(kind="message_start")
                            continue

                        if event_type == "message_delta":
                            usage = self._merge_usage(usage, event.get("usage"))
                            continue

                        if event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    yield StreamEvent(kind="text_delta", text=text)
                            elif delta_type == "thinking_delta":
                                thinking = delta.get("thinking")
                                if isinstance(thinking, str) and thinking:
                                    yield StreamEvent(kind="thinking_delta", text=thinking)
                            continue

                        if event_type == "message_stop":
                            saw_end = True
                            yield StreamEvent(kind="message_end", usage=usage)
                            return

                        if event_type == "error":
                            error = event.get("error", {})
                            message = "Claude 返回了错误事件。"
                            if isinstance(error, dict):
                                raw_message = error.get("message")
                                if isinstance(raw_message, str) and raw_message.strip():
                                    message = raw_message.strip()
                            raise ProviderResponseError(message)

                    if not saw_end:
                        yield StreamEvent(kind="message_end", usage=usage)
        except ProviderResponseError:
            raise
        except Exception as exc:
            if isinstance(exc, ProviderRequestError):
                raise
            if isinstance(exc, httpx.HTTPError):
                raise self.map_request_error(exc) from exc
            raise

    def _build_payload(self, request: ChatRequest) -> dict:
        system_messages = [
            message.content for message in request.messages if message.role == "system"
        ]
        chat_messages = [
            self._serialize_message(message)
            for message in request.messages
            if message.role in {"user", "assistant"}
        ]

        payload: dict[str, object] = {
            "model": request.model,
            "messages": chat_messages,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": True,
        }
        if system_messages:
            payload["system"] = "\n\n".join(system_messages)
        if request.thinking and request.thinking.enabled:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": request.thinking.budget_tokens or DEFAULT_THINKING_BUDGET,
            }
        return payload

    @staticmethod
    def _merge_usage(current: MessageUsage, raw_usage: object) -> MessageUsage:
        if not isinstance(raw_usage, dict):
            return current

        incoming = ClaudeProvider.build_usage(
            raw_usage,
            input_keys=("input_tokens", "prompt_tokens"),
            output_keys=("output_tokens", "completion_tokens"),
        )
        return MessageUsage(
            input_tokens=incoming.input_tokens or current.input_tokens,
            output_tokens=incoming.output_tokens or current.output_tokens,
        )

    @staticmethod
    def _serialize_message(message: ApiMessage) -> dict[str, str]:
        return {
            "role": message.role,
            "content": message.content,
        }
