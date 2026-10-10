from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from copy import deepcopy

import httpx

from lancher_code.errors import ProviderPromptTooLongError, ProviderRequestError, ProviderResponseError
from lancher_code.logging_system import get_logger
from lancher_code.models import ChatRequest, ContentBlock, MessageUsage, StreamEvent, ToolCallChunk
from lancher_code.providers.base import BaseChatProvider
from lancher_code.run_usage import RequestUsageStatus, UsageField, UsageObserver

logger = get_logger("providers.claude")

DEFAULT_MAX_TOKENS = 4096


class ClaudeProvider(BaseChatProvider):
    def __init__(
        self,
        config,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        *,
        usage_observer: UsageObserver | None = None,
    ) -> None:
        super().__init__(config=config, client_factory=client_factory, usage_observer=usage_observer)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        request = self._prepare_usage_attempt(request)
        url = f"{self.config.base_url.rstrip('/')}/messages"
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = self._build_payload(request)

        saw_end = False
        usage = MessageUsage(is_final=False)
        usage_parts: dict[str, int] = {}
        saw_output_delta = False
        stop_reason: str | None = None
        usage_request_id: str | None = None
        usage_status: RequestUsageStatus = "failed"
        assistant_blocks: dict[int, ContentBlock] = {}
        tool_arguments: dict[int, str] = {}
        try:
            async with self._client_factory() as client:
                usage_request_id = self._start_usage_request(request)
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
                                usage, fields = self._merge_usage(usage_parts, message.get("usage"))
                                self._report_usage(usage_request_id, usage, fields)
                            yield StreamEvent(kind="message_start")
                            continue

                        if event_type == "message_delta":
                            usage, fields = self._merge_usage(usage_parts, event.get("usage"))
                            raw_usage = event.get("usage")
                            if isinstance(raw_usage, dict) and self._read_optional_usage_value(
                                    raw_usage, ("output_tokens", "completion_tokens")) is not None:
                                saw_output_delta = True
                            delta = event.get("delta")
                            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                                stop_reason = delta["stop_reason"]
                            self._report_usage(usage_request_id, usage, fields)
                            continue

                        if event_type == "content_block_start":
                            block = event.get("content_block", {})
                            index = int(event.get("index", 0))
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type == "thinking":
                                thinking = block.get("thinking")
                                thinking = thinking if isinstance(thinking, str) else ""
                                signature = block.get("signature")
                                assistant_blocks[index] = ContentBlock.thinking_block(
                                    thinking, signature=signature if isinstance(signature, str) else None,
                                )
                                if thinking:
                                    yield StreamEvent(kind="thinking_delta", text=thinking)
                            elif block_type == "redacted_thinking":
                                data = block.get("data")
                                if not isinstance(data, str):
                                    raise ProviderResponseError("加密思考内容块缺少 data 字符串。")
                                assistant_blocks[index] = ContentBlock.redacted_thinking_block(data)
                            elif block_type == "text":
                                text = block.get("text")
                                text = text if isinstance(text, str) else ""
                                assistant_blocks[index] = ContentBlock.text_block(text)
                                if text:
                                    yield StreamEvent(kind="text_delta", text=text)
                            elif block_type == "tool_use":
                                input_payload = block.get("input")
                                arguments_delta = ""
                                if input_payload is not None and input_payload != {}:
                                    arguments_delta = json.dumps(input_payload, ensure_ascii=False)
                                call_id = block.get("id") if isinstance(block.get("id"), str) else None
                                name = block.get("name") if isinstance(block.get("name"), str) else ""
                                assistant_blocks[index] = ContentBlock.tool_use_block(
                                    call_id=call_id or "", name=name,
                                    input=input_payload if isinstance(input_payload, dict) else {},
                                )
                                tool_arguments[index] = arguments_delta
                                yield StreamEvent(
                                    kind="tool_call_delta",
                                    tool_call_chunk=ToolCallChunk(
                                        call_index=index,
                                        provider_call_id=call_id,
                                        name_delta=name,
                                        arguments_delta=arguments_delta,
                                    ),
                                )
                            continue

                        if event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if not isinstance(delta, dict):
                                continue
                            delta_type = delta.get("type")
                            index = int(event.get("index", 0))
                            if delta_type == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    block = assistant_blocks.setdefault(index, ContentBlock.text_block(""))
                                    block.text += text
                                    yield StreamEvent(kind="text_delta", text=text)
                            elif delta_type == "thinking_delta":
                                thinking = delta.get("thinking")
                                if isinstance(thinking, str) and thinking:
                                    block = assistant_blocks.setdefault(index, ContentBlock.thinking_block(""))
                                    block.text += thinking
                                    yield StreamEvent(kind="thinking_delta", text=thinking)
                            elif delta_type == "signature_delta":
                                signature = delta.get("signature")
                                if isinstance(signature, str):
                                    block = assistant_blocks.get(index)
                                    if block is None or block.kind != "thinking":
                                        raise ProviderResponseError("思考签名增量缺少对应的思考内容块。")
                                    block.signature = (block.signature or "") + signature
                            elif delta_type == "input_json_delta":
                                partial_json = delta.get("partial_json")
                                if isinstance(partial_json, str) and partial_json:
                                    tool_arguments[index] = tool_arguments.get(index, "") + partial_json
                                    yield StreamEvent(
                                        kind="tool_call_delta",
                                        tool_call_chunk=ToolCallChunk(
                                            call_index=index,
                                            arguments_delta=partial_json,
                                        ),
                                    )
                            continue

                        if event_type == "message_stop":
                            saw_end = True
                            usage_status = "completed"
                            usage.is_final = saw_output_delta
                            self._report_usage(usage_request_id, usage, usage.known_fields)
                            yield StreamEvent(kind="message_end", usage=usage, stop_reason=stop_reason,
                                              assistant_blocks=self._complete_assistant_blocks(assistant_blocks, tool_arguments),
                                              response_complete=True)
                            return

                        if event_type == "error":
                            error = event.get("error", {})
                            message = "Claude 返回了错误事件。"
                            if isinstance(error, dict):
                                raw_message = error.get("message")
                                if isinstance(raw_message, str) and raw_message.strip():
                                    message = raw_message.strip()
                                raw_code = error.get("type") or error.get("code")
                                if self.is_prompt_too_long(
                                    message,
                                    code=raw_code if isinstance(raw_code, str) else None,
                                ):
                                    raise ProviderPromptTooLongError(message)
                            raise ProviderResponseError(message)

                    if not saw_end:
                        usage_status = "incomplete"
                        yield StreamEvent(kind="message_end", usage=usage, stop_reason=stop_reason,
                                          response_complete=False)
        except (asyncio.CancelledError, GeneratorExit):
            if usage_status not in {"completed", "incomplete"}:
                usage_status = "cancelled"
            raise
        except ProviderResponseError:
            usage_status = "failed"
            logger.exception("event=provider_response_failed provider=claude")
            raise
        except Exception as exc:
            usage_status = "failed"
            logger.exception(
                "event=provider_request_failed provider=claude exception_type=%s",
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
        blocks: dict[int, ContentBlock], tool_arguments: dict[int, str],
    ) -> list[ContentBlock]:
        completed = deepcopy(blocks)
        for index, block in completed.items():
            if block.kind != "tool_use":
                continue
            if not block.call_id:
                raise ProviderResponseError("提供方工具调用缺少真实调用标识，不能回传或执行。")
            raw = tool_arguments.get(index, "")
            try:
                value = json.loads(raw.strip() or "{}")
            except json.JSONDecodeError:
                value = None
            # 保留真实调用标识和原文，让下一轮收到解析失败反馈而不是伪造调用。
            block.input = value if isinstance(value, dict) else {"INVALID_JSON": raw}
        return [completed[index] for index in sorted(completed)]

    def _build_payload(self, request: ChatRequest) -> dict[str, object]:
        max_tokens = request.max_output_tokens if request.max_output_tokens is not None else DEFAULT_MAX_TOKENS
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ProviderRequestError("输出 token 上限必须为正整数。")
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [self._serialize_message(message) for message in request.messages],
            "max_tokens": max_tokens,
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
            budget = request.thinking.effective_budget_tokens
            max_tokens = request.max_output_tokens if request.max_output_tokens is not None else DEFAULT_MAX_TOKENS
            if type(budget) is not int or budget <= 0 or budget >= max_tokens:
                raise ProviderRequestError("思考 token 预算必须为正整数，且小于本次输出上限。")
            return {
                "type": "enabled",
                "budget_tokens": budget,
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
            elif message.role == "assistant" and block.thinking_protocol == "claude":
                if block.kind == "thinking":
                    thinking: dict[str, object] = {"type": "thinking", "thinking": block.text}
                    if block.signature is not None:
                        thinking["signature"] = block.signature
                    content.append(thinking)
                elif block.kind == "redacted_thinking":
                    content.append({"type": "redacted_thinking", "data": block.data})
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
    def _merge_usage(
        parts: dict[str, int], raw_usage: object
    ) -> tuple[MessageUsage, frozenset[UsageField]]:
        if isinstance(raw_usage, dict):
            for name, keys in (
                ("input", ("input_tokens", "prompt_tokens")),
                ("output", ("output_tokens", "completion_tokens")),
                ("cache", ("cache_read_input_tokens", "cached_input_tokens")),
                ("creation", ("cache_creation_input_tokens",)),
                ("reasoning", ("reasoning_tokens", "output_tokens_details.reasoning_tokens")),
            ):
                value = ClaudeProvider._read_optional_usage_value(raw_usage, keys)
                if value is not None:
                    parts[name] = value
        # 各输入分量分别保留；后续只有输出的帧不能抹掉缓存创建或读取量。
        total_input = (parts["input"] + parts.get("cache", 0) + parts.get("creation", 0)
                       if "input" in parts else None)
        partial = frozenset({"input"}) if "input" in parts and not {"cache", "creation"} <= parts.keys() else frozenset()
        usage = MessageUsage(input_tokens=total_input, cached_input_tokens=parts.get("cache"),
                             output_tokens=parts.get("output"), cache_creation_input_tokens=parts.get("creation"),
                             reasoning_output_tokens=parts.get("reasoning"), is_final=False, partial_fields=partial)
        return usage, usage.known_fields
