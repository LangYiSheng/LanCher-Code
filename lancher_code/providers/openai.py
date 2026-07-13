from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from lancher_code.errors import ProviderRequestError, ProviderResponseError
from lancher_code.logging_system import get_logger
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolCallChunk
from lancher_code.providers.base import BaseChatProvider

logger = get_logger("providers.openai")


class OpenAIProvider(BaseChatProvider):
    def __init__(
        self,
        config,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__(config=config, client_factory=client_factory)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = self._build_payload(request)

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
                                cached_input_keys=("prompt_tokens_details.cached_tokens",),
                            )

                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                yield StreamEvent(kind="text_delta", text=content)

                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(reasoning, str) and reasoning:
                                yield StreamEvent(kind="thinking_delta", text=reasoning)

                            tool_calls = delta.get("tool_calls")
                            if isinstance(tool_calls, list):
                                for tool_call in tool_calls:
                                    if not isinstance(tool_call, dict):
                                        continue
                                    function = tool_call.get("function", {})
                                    if not isinstance(function, dict):
                                        function = {}
                                    name_delta = function.get("name")
                                    arguments_delta = function.get("arguments")
                                    if not isinstance(name_delta, str):
                                        name_delta = ""
                                    if not isinstance(arguments_delta, str):
                                        arguments_delta = ""
                                    if not name_delta and not arguments_delta and not tool_call.get("id"):
                                        continue
                                    yield StreamEvent(
                                        kind="tool_call_delta",
                                        tool_call_chunk=ToolCallChunk(
                                            call_index=int(tool_call.get("index", 0)),
                                            provider_call_id=tool_call.get("id")
                                            if isinstance(tool_call.get("id"), str)
                                            else None,
                                            name_delta=name_delta,
                                            arguments_delta=arguments_delta,
                                        ),
                                    )

                    yield StreamEvent(kind="message_end", usage=usage)
        except ProviderResponseError:
            logger.exception("event=provider_response_failed provider=openai")
            raise
        except Exception as exc:
            logger.exception(
                "event=provider_request_failed provider=openai exception_type=%s",
                type(exc).__name__,
            )
            if isinstance(exc, ProviderRequestError):
                raise
            if isinstance(exc, httpx.HTTPError):
                raise self.map_request_error(exc) from exc
            raise

    def _build_payload(self, request: ChatRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [self._serialize_system_message(text) for text in request.system]
            + [self._serialize_message(message) for message in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.allow_tool_calls and request.tools:
            payload["tools"] = [self._serialize_tool(tool) for tool in request.tools]
        return payload

    @staticmethod
    def _serialize_system_message(text: str) -> dict[str, object]:
        return {
            "role": "system",
            "content": text,
        }

    def _serialize_message(self, message) -> dict[str, object]:
        if message.role == "tool":
            block = message.blocks[0]
            return {
                "role": "tool",
                "tool_call_id": block.call_id,
                "content": block.text,
            }

        tool_use_blocks = [block for block in message.blocks if block.kind == "tool_use"]
        text_content = self.text_from_blocks(message.blocks)
        if message.role == "assistant" and tool_use_blocks:
            return {
                "role": "assistant",
                "content": self._serialize_text_content(message.blocks),
                "tool_calls": [
                    {
                        "id": block.call_id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input, ensure_ascii=False),
                        },
                    }
                    for block in tool_use_blocks
                ],
            }

        return {
            "role": message.role,
            "content": self._serialize_text_content(message.blocks),
        }

    @staticmethod
    def _serialize_text_content(blocks) -> str | list[dict[str, str]]:
        text_blocks = [block for block in blocks if block.kind == "text"]
        if not text_blocks:
            return ""
        if len(text_blocks) == 1:
            return text_blocks[0].text
        return [{"type": "text", "text": block.text} for block in text_blocks]

    @staticmethod
    def _serialize_tool(tool) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
