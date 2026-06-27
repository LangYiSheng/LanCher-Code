from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx

from lancher_code.errors import ProviderRequestError, ProviderResponseError
from lancher_code.models import ApiMessage, ChatRequest, MessageUsage, ProviderConfig, StreamEvent
from lancher_code.providers.base import BaseChatProvider


class OpenAIProvider(BaseChatProvider):
    def __init__(
        self,
        config: ProviderConfig,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__(config=config, client_factory=client_factory)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": request.model,
            "messages": [self._serialize_message(message) for message in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        usage = MessageUsage()
        try:
            async with self._client_factory() as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await self.raise_for_error_status(response)
                    yield StreamEvent(kind="message_start")

                    async for _event_name, data in self.iter_sse_events(response):
                        if data == "[DONE]":
                            yield StreamEvent(kind="message_end", usage=usage)
                            return

                        chunk = self.parse_json_payload(data)
                        if "error" in chunk:
                            raise ProviderResponseError("OpenAI 响应包含 error 字段。")

                        if isinstance(chunk.get("usage"), dict):
                            usage = self.build_usage(
                                chunk["usage"],
                                input_keys=("prompt_tokens", "input_tokens"),
                                output_keys=("completion_tokens", "output_tokens"),
                            )

                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                yield StreamEvent(kind="text_delta", text=content)

                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(reasoning, str) and reasoning:
                                yield StreamEvent(kind="thinking_delta", text=reasoning)

                    yield StreamEvent(kind="message_end", usage=usage)
        except ProviderResponseError:
            raise
        except Exception as exc:
            if isinstance(exc, ProviderRequestError):
                raise
            if isinstance(exc, httpx.HTTPError):
                raise self.map_request_error(exc) from exc
            raise

    @staticmethod
    def _serialize_message(message: ApiMessage) -> dict[str, str]:
        return {
            "role": message.role,
            "content": message.content,
        }
