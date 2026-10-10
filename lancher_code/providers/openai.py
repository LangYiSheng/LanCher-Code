from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from urllib.parse import urlsplit

import httpx

from lancher_code.errors import ProviderPromptTooLongError, ProviderRequestError, ProviderResponseError
from lancher_code.logging_system import get_logger
from lancher_code.contracts.messages import ChatRequest, ContentBlock, StreamEvent
from lancher_code.usage.models import MessageUsage, merge_usage
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.providers.base import BaseChatProvider
from lancher_code.usage.ledger import RequestUsageStatus

logger = get_logger("providers.openai")


class OpenAIProvider(BaseChatProvider):
    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        request = self._prepare_usage_attempt(request)
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = self._build_payload(request)

        usage = MessageUsage(is_final=False)
        stop_reason: str | None = None
        usage_request_id: str | None = None
        usage_status: RequestUsageStatus = "failed"
        assistant_blocks: list[ContentBlock] = []
        tool_blocks: dict[int, ContentBlock] = {}
        tool_arguments: dict[int, str] = {}
        try:
            async with self._client_factory() as client:
                usage_request_id = self._start_usage_request(request)
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await self.raise_for_error_status(response)
                    yield StreamEvent(kind="message_start")

                    async for _event_name, data in self.iter_sse_events(response):
                        if data == "[DONE]":
                            usage_status = "completed"
                            usage.is_final = True
                            self._report_usage(usage_request_id, usage, usage.known_fields)
                            yield StreamEvent(kind="message_end", usage=usage, stop_reason=stop_reason,
                                              assistant_blocks=self._complete_assistant_blocks(assistant_blocks, tool_blocks,
                                                                                               tool_arguments),
                                              response_complete=True)
                            return

                        chunk = self.parse_json_payload(data)
                        if "error" in chunk:
                            error = chunk.get("error")
                            if isinstance(error, dict):
                                message = str(error.get("message", ""))
                                code = error.get("code") or error.get("type")
                                if self.is_prompt_too_long(message, code=code if isinstance(code, str) else None):
                                    raise ProviderPromptTooLongError(message or "请求超过模型上下文窗口。")
                            raise ProviderResponseError("OpenAI 响应包含 error 字段。")

                        if isinstance(chunk.get("usage"), dict):
                            usage_keys = dict(
                                input_keys=("prompt_tokens", "input_tokens"),
                                output_keys=("completion_tokens", "output_tokens"),
                                cached_input_keys=("prompt_tokens_details.cached_tokens", "prompt_cache_hit_tokens",
                                                   "cached_input_tokens"),
                                cache_creation_keys=("cache_creation_input_tokens",),
                                reasoning_keys=("completion_tokens_details.reasoning_tokens", "reasoning_tokens"),
                            )
                            fields = self.usage_fields(chunk["usage"], **usage_keys)
                            incoming = self.build_usage(
                                chunk["usage"],
                                **usage_keys,
                            )
                            usage = merge_usage(usage, incoming)
                            self._report_usage(usage_request_id, usage, fields)

                        for choice in chunk.get("choices", []):
                            reason = choice.get("finish_reason")
                            if isinstance(reason, str):
                                stop_reason = reason
                            delta = choice.get("delta", {})
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                if assistant_blocks and assistant_blocks[-1].kind == "text":
                                    assistant_blocks[-1].text += content
                                else:
                                    assistant_blocks.append(ContentBlock.text_block(content))
                                yield StreamEvent(kind="text_delta", text=content)

                            for reasoning_field in ("reasoning_content", "reasoning"):
                                reasoning = delta.get(reasoning_field)
                                if isinstance(reasoning, str) and reasoning:
                                    if (assistant_blocks and assistant_blocks[-1].kind == "thinking"
                                            and assistant_blocks[-1].thinking_field == reasoning_field):
                                        assistant_blocks[-1].text += reasoning
                                    else:
                                        assistant_blocks.append(ContentBlock.thinking_block(
                                            reasoning, protocol="openai", thinking_field=reasoning_field,
                                        ))
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
                                    index = int(tool_call.get("index", 0))
                                    call_id = tool_call.get("id") if isinstance(tool_call.get("id"), str) else None
                                    block = tool_blocks.get(index)
                                    if block is None:
                                        block = ContentBlock.tool_use_block(call_id=call_id or "",
                                                                            name="", input={})
                                        tool_blocks[index] = block
                                        assistant_blocks.append(block)
                                    elif call_id:
                                        block.call_id = call_id
                                    block.name += name_delta
                                    tool_arguments[index] = tool_arguments.get(index, "") + arguments_delta
                                    yield StreamEvent(
                                        kind="tool_call_delta",
                                        tool_call_chunk=ToolCallChunk(
                                            call_index=index,
                                            provider_call_id=call_id,
                                            name_delta=name_delta,
                                            arguments_delta=arguments_delta,
                                        ),
                                    )

                    usage_status = "incomplete"
                    yield StreamEvent(kind="message_end", usage=usage, stop_reason=stop_reason,
                                      response_complete=False)
        except (asyncio.CancelledError, GeneratorExit):
            if usage_status not in {"completed", "incomplete"}:
                usage_status = "cancelled"
            raise
        except ProviderResponseError:
            usage_status = "failed"
            logger.exception("event=provider_response_failed provider=openai")
            raise
        except Exception as exc:
            usage_status = "failed"
            logger.exception(
                "event=provider_request_failed provider=openai exception_type=%s",
                type(exc).__name__,
            )
            if isinstance(exc, ProviderRequestError):
                raise
            if isinstance(exc, httpx.HTTPError):
                raise self.map_request_error(exc) from exc
            raise
        finally:
            self._finish_usage_request(usage_request_id, usage_status)

    @staticmethod
    def _complete_assistant_blocks(
        blocks: list[ContentBlock], tools: dict[int, ContentBlock], arguments: dict[int, str],
    ) -> list[ContentBlock]:
        for index, block in tools.items():
            if not block.call_id:
                raise ProviderResponseError("提供方工具调用缺少真实调用标识，不能回传或执行。")
            raw = arguments.get(index, "")
            try:
                value = json.loads(raw.strip() or "{}")
            except json.JSONDecodeError:
                value = None
            block.input = value if isinstance(value, dict) else {"INVALID_JSON": raw}
        return deepcopy(blocks)

    def _build_payload(self, request: ChatRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [self._serialize_system_message(text) for text in request.system]
            + [item for message in request.messages for item in self._serialize_messages(message)],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.allow_tool_calls and request.tools:
            payload["tools"] = [self._serialize_tool(tool) for tool in request.tools]
        if request.max_output_tokens is not None:
            if type(request.max_output_tokens) is not int or request.max_output_tokens <= 0:
                raise ProviderRequestError("输出 token 上限必须为正整数。")
            # 官方接口的新参数包含推理输出；兼容端点继续使用其通用旧字段。
            limit_field = ("max_completion_tokens" if urlsplit(self.config.base_url).hostname == "api.openai.com"
                           else "max_tokens")
            payload[limit_field] = request.max_output_tokens
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
        serialized: dict[str, object] = {
            "role": message.role,
            "content": self._serialize_text_content(message.blocks),
        }
        if message.role == "assistant" and tool_use_blocks:
            serialized["tool_calls"] = [
                {
                    "id": block.call_id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    },
                }
                for block in tool_use_blocks
            ]

        if message.role == "assistant":
            for block in message.blocks:
                if block.kind == "thinking" and block.thinking_protocol == "openai" and block.text:
                    field = block.thinking_field or "reasoning_content"
                    serialized[field] = str(serialized.get(field, "")) + block.text
        return serialized

    def _serialize_messages(self, message) -> list[dict[str, object]]:
        if message.role == "tool":
            # 统一历史会把并行结果放在同一条消息，OpenAI 要求逐条回复每个调用。
            return [
                {"role": "tool", "tool_call_id": block.call_id, "content": block.text}
                for block in message.blocks
                if block.kind == "tool_result"
            ]
        return [self._serialize_message(message)]

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
