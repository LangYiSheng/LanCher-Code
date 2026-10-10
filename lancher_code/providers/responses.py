"""实验工具追加使用真实 Responses 协议，常规请求保持 Chat Completions。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import asdict

import httpx

from lancher_code.contracts.messages import ChatRequest, ContentBlock, StreamEvent
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.errors import ProviderPromptTooLongError, ProviderRequestError, ProviderResponseError
from lancher_code.logging_system import get_logger
from lancher_code.providers.base import BaseChatProvider
from lancher_code.providers.native_tools import (
    APPEND_FALLBACK, grouped_tool_updates, raise_native_error_status, responses_tool,
)
from lancher_code.usage.ledger import RequestUsageStatus
from lancher_code.usage.models import MessageUsage, merge_usage


logger = get_logger("providers.responses")
USAGE_KEYS = dict(input_keys=("input_tokens",), output_keys=("output_tokens",),
                  cached_input_keys=("input_tokens_details.cached_tokens",),
                  reasoning_keys=("output_tokens_details.reasoning_tokens",))


class ResponsesProvider(BaseChatProvider):
    def _build_payload(self, request: ChatRequest) -> dict[str, object]:
        updates = grouped_tool_updates(request)
        definitions = {tool.name: responses_tool(asdict(tool)) for tool in request.tools}
        items: list[dict[str, object]] = [{"role": "developer", "content": text} for text in request.system]
        for index in range(len(request.messages) + 1):
            for event in updates.get(index, []):
                if event["removals"]:
                    raise ProviderRequestError(f"Responses additional_tools 不支持移除工具；需重建工具基线。{APPEND_FALLBACK}")
                additions = []
                for raw in event["additions"]:
                    tool = responses_tool(raw)
                    previous = definitions.get(tool["name"])
                    if previous is not None and previous != tool:
                        raise ProviderRequestError(f"Responses additional_tools 不支持同名工具定义替换；需重建工具基线。{APPEND_FALLBACK}")
                    definitions[tool["name"]] = tool
                    additions.append(tool)
                if additions:
                    items.append({"type": "additional_tools", "role": "developer", "tools": additions})
            if index < len(request.messages):
                items.extend(self._serialize_message(request.messages[index]))
        payload: dict[str, object] = {"model": request.model, "input": items, "stream": True, "store": False,
                                     "include": ["reasoning.encrypted_content"]}
        if request.tools:
            payload["tools"] = [responses_tool(asdict(tool)) for tool in request.tools]
        if not request.allow_tool_calls:
            payload["tool_choice"] = "none"
        if request.max_output_tokens is not None:
            if type(request.max_output_tokens) is not int or request.max_output_tokens <= 0:
                raise ProviderRequestError("输出 token 上限必须为正整数。")
            payload["max_output_tokens"] = request.max_output_tokens
        # 不把 Claude 的 token 思考预算硬映射成 Responses 的 reasoning.effort。
        return payload

    @staticmethod
    def _serialize_message(message) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        text: list[dict[str, object]] = []
        role = "developer" if message.role == "system" else message.role

        def flush() -> None:
            if text:
                # EasyInputMessage 的助手文本使用字符串，不能伪造缺少 id/status 的 output message。
                content = "".join(part["text"] for part in text) if role == "assistant" else list(text)
                items.append({"role": role, "content": content})
                text.clear()

        for block in message.blocks:
            if block.kind == "text":
                text.append({"type": "input_text", "text": block.text})
            elif block.kind == "tool_result":
                flush()
                items.append({"type": "function_call_output", "call_id": block.call_id, "output": block.text})
            elif block.kind == "tool_use":
                flush()
                items.append({"type": "function_call", "call_id": block.call_id, "name": block.name,
                              "arguments": json.dumps(block.input, ensure_ascii=False)})
            elif block.kind == "thinking" and block.thinking_protocol == "openai" and block.data:
                flush()
                try:
                    item = json.loads(block.data)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ProviderRequestError("保存的 Responses reasoning 项不是合法 JSON。") from exc
                if not isinstance(item, dict) or item.get("type") != "reasoning":
                    raise ProviderRequestError("保存的 Responses reasoning 项类型不合法。")
                items.append(item)
        flush()
        return items

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        request = self._prepare_usage_attempt(request)
        payload = self._build_payload(request)
        url = f"{self.config.base_url.rstrip('/')}/responses"
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        usage = MessageUsage(is_final=False)
        usage_request_id: str | None = None
        usage_status: RequestUsageStatus = "failed"
        tools: dict[int, dict[str, str]] = {}
        texts: dict[tuple[int, int], str] = {}
        try:
            async with self._client_factory() as client:
                usage_request_id = self._start_usage_request(request)
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    await raise_native_error_status(response, experimental=True)
                    yield StreamEvent(kind="message_start")
                    async for event_name, data in self.iter_sse_events(response):
                        if data == "[DONE]":
                            break
                        event = self.parse_json_payload(data)
                        kind = event.get("type", event_name)
                        index = int(event.get("output_index", 0))
                        if kind in {"response.output_text.delta", "response.refusal.delta"}:
                            delta = event.get("delta")
                            if isinstance(delta, str) and delta:
                                key = (index, int(event.get("content_index", 0)))
                                texts[key] = texts.get(key, "") + delta
                                yield StreamEvent(kind="text_delta", text=delta)
                        elif kind == "response.reasoning_summary_text.delta":
                            delta = event.get("delta")
                            if isinstance(delta, str) and delta:
                                yield StreamEvent(kind="thinking_delta", text=delta)
                        elif kind == "response.output_item.added":
                            item = event.get("item")
                            if isinstance(item, dict) and item.get("type") == "function_call":
                                state = tools.setdefault(index, {"call_id": "", "name": "", "arguments": ""})
                                call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments", "")
                                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                                    raise ProviderResponseError("Responses 工具调用缺少真实调用标识或名称。")
                                if state["call_id"] or not isinstance(arguments, str):
                                    raise ProviderResponseError("Responses 工具调用起始帧重复或格式错误。")
                                state.update(call_id=call_id, name=name, arguments=arguments)
                                yield self._tool_delta(index, call_id, name, arguments)
                        elif kind == "response.function_call_arguments.delta":
                            delta = event.get("delta")
                            if isinstance(delta, str) and delta:
                                state = tools.setdefault(index, {"call_id": "", "name": "", "arguments": ""})
                                state["arguments"] += delta
                                yield self._tool_delta(index, None, "", delta)
                        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                            final = event.get("response")
                            if not isinstance(final, dict):
                                raise ProviderResponseError("Responses 结束帧缺少 response 对象。")
                            raw_usage = final.get("usage")
                            if isinstance(raw_usage, dict):
                                fields = self.usage_fields(raw_usage, **USAGE_KEYS)
                                usage = merge_usage(usage, self.build_usage(raw_usage, **USAGE_KEYS))
                                usage.is_final = True
                                self._report_usage(usage_request_id, usage, fields)
                            if kind == "response.failed":
                                self._raise_error(final.get("error"))
                            if kind == "response.incomplete":
                                usage_status = "incomplete"
                                details = final.get("incomplete_details")
                                reason = details.get("reason") if isinstance(details, dict) else "incomplete"
                                yield StreamEvent(kind="message_end", usage=usage, stop_reason=str(reason),
                                                  response_complete=False)
                                return
                            if final.get("status", "completed") != "completed":
                                raise ProviderResponseError("Responses 结束事件与响应状态不一致。")
                            output = final.get("output")
                            if not isinstance(output, list):
                                raise ProviderResponseError("Responses 完整结束帧缺少 output 列表。")
                            blocks = self._complete_blocks(output)
                            for output_index, item in enumerate(output):
                                if item.get("type") == "function_call":
                                    state = tools.get(output_index, {"call_id": "", "name": "", "arguments": ""})
                                    if (state["call_id"] and state["call_id"] != item["call_id"]
                                            or state["name"] and state["name"] != item["name"]
                                            or not item["arguments"].startswith(state["arguments"])):
                                        raise ProviderResponseError("Responses 完整工具调用与流式增量不一致。")
                                    suffix = item["arguments"][len(state["arguments"]):]
                                    if not state["call_id"] or not state["name"] or suffix:
                                        yield self._tool_delta(output_index, item["call_id"],
                                                               "" if state["name"] else item["name"], suffix)
                                elif item.get("type") == "message":
                                    for content_index, part in enumerate(item.get("content", [])):
                                        if part.get("type") not in {"output_text", "refusal"}:
                                            continue
                                        complete_text = part["text"] if part["type"] == "output_text" else part["refusal"]
                                        prior = texts.get((output_index, content_index), "")
                                        if not complete_text.startswith(prior):
                                            raise ProviderResponseError("Responses 完整文本与流式增量不一致。")
                                        suffix = complete_text[len(prior):]
                                        if suffix:
                                            yield StreamEvent(kind="text_delta", text=suffix)
                            if set(tools) - {i for i, item in enumerate(output) if item.get("type") == "function_call"}:
                                raise ProviderResponseError("Responses 完整响应遗漏了已开始的工具调用。")
                            usage_status = "completed"
                            yield StreamEvent(kind="message_end", usage=usage,
                                              stop_reason="tool_calls" if any(b.kind == "tool_use" for b in blocks) else "stop",
                                              assistant_blocks=blocks, response_complete=True)
                            return
                        elif kind == "error":
                            self._raise_error(event)
                    usage_status = "incomplete"
                    yield StreamEvent(kind="message_end", usage=usage, response_complete=False)
        except (asyncio.CancelledError, GeneratorExit):
            if usage_status not in {"completed", "incomplete"}:
                usage_status = "cancelled"
            raise
        except Exception as exc:
            usage_status = "failed"
            logger.exception("event=provider_request_failed provider=responses exception_type=%s", type(exc).__name__)
            if isinstance(exc, httpx.HTTPError):
                raise self.map_request_error(exc) from exc
            raise
        finally:
            self._finish_usage_request(usage_request_id, usage_status)

    @staticmethod
    def _tool_delta(index: int, call_id: str | None, name: str, arguments: str) -> StreamEvent:
        return StreamEvent(kind="tool_call_delta", tool_call_chunk=ToolCallChunk(
            call_index=index, provider_call_id=call_id, name_delta=name, arguments_delta=arguments))

    @staticmethod
    def _complete_blocks(output: list[dict[str, object]]) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderResponseError("Responses output 项必须是对象。")
            if item.get("status", "completed") not in {None, "completed"}:
                raise ProviderResponseError("Responses 完整响应仍包含未完成的 output 项。")
            kind = item.get("type")
            if kind == "function_call":
                call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, str):
                    raise ProviderResponseError("Responses 工具调用缺少真实调用标识、名称或参数。")
                try:
                    value = json.loads(arguments)
                except json.JSONDecodeError:
                    value = None
                blocks.append(ContentBlock.tool_use_block(call_id=call_id, name=name,
                              input=value if isinstance(value, dict) else {"INVALID_JSON": arguments}))
            elif kind == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    raise ProviderResponseError("Responses 文本消息缺少 content 列表。")
                for part in content:
                    if not isinstance(part, dict):
                        raise ProviderResponseError("Responses 文本内容块格式错误。")
                    text = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
                    if not isinstance(text, str) or part.get("type") not in {"output_text", "refusal"}:
                        raise ProviderResponseError("Responses 文本消息包含不支持的内容块。")
                    blocks.append(ContentBlock.text_block(text))
            elif kind == "reasoning":
                summary = item.get("summary", [])
                text = "".join(part.get("text", "") for part in summary
                               if isinstance(part, dict) and isinstance(part.get("text"), str)) if isinstance(summary, list) else ""
                block = ContentBlock.thinking_block(text, protocol="openai")
                block.data = json.dumps(deepcopy(item), ensure_ascii=False)
                blocks.append(block)
            else:
                # 服务端其他工具既未声明也未接入执行器，不能悄悄冒充完整可执行响应。
                raise ProviderResponseError(f"暂不支持 Responses output 类型：{kind}。{APPEND_FALLBACK}")
        return blocks

    @staticmethod
    def _raise_error(error: object) -> None:
        payload = error if isinstance(error, dict) else {}
        message = payload.get("message", "Responses 返回了错误事件。")
        code = payload.get("code") or payload.get("type")
        if BaseChatProvider.is_prompt_too_long(str(message), code=code if isinstance(code, str) else None):
            raise ProviderPromptTooLongError(str(message))
        raise ProviderResponseError(f"{message} {APPEND_FALLBACK}")
