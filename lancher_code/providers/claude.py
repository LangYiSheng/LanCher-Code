from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx

from lancher_code.errors import ProviderRequestError, ProviderResponseError
from lancher_code.logging_system import get_logger
from lancher_code.models import ChatRequest, MessageUsage, StreamEvent, ToolCallChunk
from lancher_code.providers.base import BaseChatProvider

logger = get_logger("providers.claude")

DEFAULT_MAX_TOKENS = 4096
DEFAULT_THINKING_BUDGET = 2048


class ClaudeProvider(BaseChatProvider):
    def __init__(
        self,
        config,
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

                        if event_type == "content_block_start":
                            block = event.get("content_block", {})
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                input_payload = block.get("input")
                                arguments_delta = ""
                                if isinstance(input_payload, dict) and input_payload:
                                    arguments_delta = json.dumps(input_payload, ensure_ascii=False)
                                yield StreamEvent(
                                    kind="tool_call_delta",
                                    tool_call_chunk=ToolCallChunk(
                                        call_index=int(event.get("index", 0)),
                                        provider_call_id=block.get("id") if isinstance(block.get("id"), str) else None,
                                        name_delta=block.get("name") if isinstance(block.get("name"), str) else "",
                                        arguments_delta=arguments_delta,
                                    ),
                                )
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
                            elif delta_type == "input_json_delta":
                                partial_json = delta.get("partial_json")
                                if isinstance(partial_json, str) and partial_json:
                                    yield StreamEvent(
                                        kind="tool_call_delta",
                                        tool_call_chunk=ToolCallChunk(
                                            call_index=int(event.get("index", 0)),
                                            arguments_delta=partial_json,
                                        ),
                                    )
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
            logger.exception("event=provider_response_failed provider=claude")
            raise
        except Exception as exc:
            logger.exception(
                "event=provider_request_failed provider=claude exception_type=%s",
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
            "messages": [self._serialize_message(message) for message in request.messages],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": True,
            "thinking": self._build_thinking_payload(request),
        }
        if request.system:
            payload["system"] = "\n\n".join(request.system)
        if request.allow_tool_calls and request.tools:
            payload["tools"] = [self._serialize_tool(tool) for tool in request.tools]
        return payload

    def _build_thinking_payload(self, request: ChatRequest) -> dict[str, object]:
        if request.thinking and request.thinking.enabled:
            return {
                "type": "enabled",
                "budget_tokens": request.thinking.budget_tokens or DEFAULT_THINKING_BUDGET,
            }
        return {"type": "disabled"}

    def _serialize_message(self, message) -> dict[str, object]:
        if message.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.call_id,
                        "content": block.text,
                        "is_error": block.is_error,
                    }
                    for block in message.blocks
                    if block.kind == "tool_result"
                ],
            }

        content: list[dict[str, object]] = []
        for block in message.blocks:
            if block.kind == "text":
                content.append({"type": "text", "text": block.text})
            elif block.kind == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.call_id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        return {
            "role": message.role,
            "content": content,
        }

    @staticmethod
    def _serialize_tool(tool) -> dict[str, object]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

    @staticmethod
    def _merge_usage(current: MessageUsage, raw_usage: object) -> MessageUsage:
        if not isinstance(raw_usage, dict):
            return current

        incoming = ClaudeProvider.build_usage(
            raw_usage,
            input_keys=("input_tokens", "prompt_tokens"),
            output_keys=("output_tokens", "completion_tokens"),
            cached_input_keys=("cache_read_input_tokens", "cached_input_tokens"),
        )
        cache_creation_input_tokens = ClaudeProvider._read_usage_value(raw_usage, ("cache_creation_input_tokens",))
        total_input_tokens = incoming.input_tokens + incoming.cached_input_tokens + cache_creation_input_tokens
        return MessageUsage(
            input_tokens=total_input_tokens or current.input_tokens,
            cached_input_tokens=incoming.cached_input_tokens or current.cached_input_tokens,
            output_tokens=incoming.output_tokens or current.output_tokens,
        )
