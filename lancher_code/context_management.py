from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from lancher_code.errors import ContextCompactionError, ProviderPromptTooLongError
from lancher_code.logging_system import get_logger
from lancher_code.models import (
    CancellationToken,
    ChatRequest,
    ContentBlock,
    ContextFileSnapshot,
    ContextManagementState,
    ContextUsageAnchor,
    ConversationMessage,
    MessageUsage,
    ToolDefinition,
    ToolResultReplacement,
)
from lancher_code.providers.base import ChatProvider


logger = get_logger("context_management")

SINGLE_TOOL_RESULT_BYTES = 50_000
TOOL_BATCH_BYTES = 200_000
TOOL_PREVIEW_LINES = 20
TOOL_PREVIEW_BYTES = 2_048
SUMMARY_OUTPUT_RESERVE = 20_000
AUTOMATIC_MARGIN = 13_000
EMERGENCY_MARGIN = 3_000
RECENT_HISTORY_TOKENS = 10_000
RECENT_HISTORY_MESSAGES = 5
RECENT_FILE_LIMIT = 5
RECENT_FILE_TOKENS = 5_000
AUTOMATIC_FAILURE_LIMIT = 3
CHARACTERS_PER_TOKEN = 3.5

SUMMARY_HEADINGS = (
    "主要请求和意图",
    "关键技术概念",
    "文件和代码段",
    "错误与修复",
    "问题解决过程",
    "用户消息与明确反馈",
    "待办任务",
    "当前工作",
    "可能的下一步",
)

SUMMARY_SYSTEM_PROMPT = """你负责压缩一段编程助手会话。只输出一个 <summary>...</summary> 标签，不得输出标签外文本或隐藏推理。
标签内必须严格按以下顺序包含九个 Markdown 二级标题：
## 主要请求和意图
## 关键技术概念
## 文件和代码段
## 错误与修复
## 问题解决过程
## 用户消息与明确反馈
## 待办任务
## 当前工作
## 可能的下一步
保留关键用户原话、当前状态、重要文件、工具结果结论和未解决错误。需要精确原文时，应提示后续重新读取，不要猜测。"""

_SUMMARY_PATTERN = re.compile(r"\A<summary>(?P<body>.*?)</summary>\Z", re.DOTALL)


@dataclass(slots=True, frozen=True)
class ToolOffloadResult:
    transcript: list[ConversationMessage]
    offloaded_count: int


@dataclass(slots=True, frozen=True)
class TranscriptCompaction:
    transcript: list[ConversationMessage]
    dropped_groups: int


def estimate_tokens_from_characters(character_count: int) -> int:
    return math.ceil(max(0, character_count) / CHARACTERS_PER_TOKEN)


def estimate_text_tokens(text: str) -> int:
    return estimate_tokens_from_characters(len(text))


def estimate_request_tokens(request: ChatRequest, state: ContextManagementState) -> int:
    serialized, shape_digest, messages_digest = _serialize_request(request)
    character_count = len(serialized)
    anchor = state.usage_anchor
    if anchor is None or anchor.system_tools_digest != shape_digest:
        return estimate_tokens_from_characters(character_count)
    if len(request.messages) < anchor.message_count:
        return estimate_tokens_from_characters(character_count)
    prefix_digest = _digest(_canonical([asdict(item) for item in request.messages[: anchor.message_count]]))
    if prefix_digest != anchor.messages_digest or character_count < anchor.request_char_count:
        return estimate_tokens_from_characters(character_count)
    return anchor.token_count + estimate_tokens_from_characters(character_count - anchor.request_char_count)


def update_usage_anchor(
    state: ContextManagementState,
    request: ChatRequest,
    usage: MessageUsage,
) -> None:
    serialized, shape_digest, messages_digest = _serialize_request(request)
    state.usage_anchor = ContextUsageAnchor(
        token_count=usage.input_tokens + usage.output_tokens,
        request_char_count=len(serialized),
        system_tools_digest=shape_digest,
        message_count=len(request.messages),
        messages_digest=messages_digest,
    )


async def offload_tool_results(
    transcript: list[ConversationMessage],
    state: ContextManagementState,
    project_root: Path,
) -> ToolOffloadResult:
    candidate = copy.deepcopy(transcript)
    result_blocks: dict[str, ContentBlock] = {}
    result_order: dict[str, int] = {}
    batches: list[list[str]] = []
    call_to_batch: dict[str, int] = {}

    for message in candidate:
        if message.role == "assistant":
            call_ids = [block.call_id for block in message.blocks if block.kind == "tool_use" and block.call_id]
            if call_ids:
                batch_index = len(batches)
                batches.append(call_ids)
                for call_id in call_ids:
                    call_to_batch.setdefault(call_id, batch_index)
        for block in message.blocks:
            if message.role == "tool" and block.kind == "tool_result" and block.call_id:
                if block.call_id not in result_blocks:
                    result_order[block.call_id] = len(result_order)
                    result_blocks[block.call_id] = block

    for call_id, replacement in state.replacements.items():
        block = result_blocks.get(call_id)
        if block is not None:
            block.text = replacement.preview

    new_ids = [call_id for call_id in result_blocks if call_id not in state.seen_call_ids]
    byte_sizes = {call_id: len(result_blocks[call_id].text.encode("utf-8")) for call_id in new_ids}
    selected = {call_id for call_id in new_ids if byte_sizes[call_id] > SINGLE_TOOL_RESULT_BYTES}

    for batch in batches:
        remaining = [call_id for call_id in batch if call_id in byte_sizes and call_id not in selected]
        total = sum(byte_sizes[call_id] for call_id in remaining)
        if total <= TOOL_BATCH_BYTES:
            continue
        for call_id in sorted(remaining, key=lambda item: (-byte_sizes[item], result_order[item])):
            selected.add(call_id)
            total -= byte_sizes[call_id]
            if total <= TOOL_BATCH_BYTES:
                break

    offloaded_count = 0
    for call_id in new_ids:
        block = result_blocks[call_id]
        if call_id not in call_to_batch:
            logger.warning("event=orphan_tool_result context_id=%s call_id=%s", state.context_id, call_id)
        if call_id not in selected:
            state.seen_call_ids.add(call_id)
            continue
        try:
            replacement = await _write_tool_result(project_root, state.context_id, call_id, block.text)
        except OSError as exc:
            logger.warning(
                "event=tool_result_offload_failed context_id=%s call_id=%s error=%s",
                state.context_id,
                call_id,
                exc,
            )
            continue
        state.replacements[call_id] = replacement
        state.seen_call_ids.add(call_id)
        block.text = replacement.preview
        offloaded_count += 1

    if offloaded_count:
        logger.info(
            "event=tool_results_offloaded context_id=%s count=%s",
            state.context_id,
            offloaded_count,
        )
    return ToolOffloadResult(transcript=candidate, offloaded_count=offloaded_count)


async def compact_transcript(
    *,
    provider: ChatProvider,
    model: str,
    transcript: list[ConversationMessage],
    visible_tools: list[ToolDefinition],
    state: ContextManagementState,
    context_window: int,
    cancellation_token: CancellationToken | None = None,
) -> TranscriptCompaction:
    transcript = _without_dynamic_reminders(transcript)
    groups = group_complete_turns(transcript)
    if not groups:
        raise ContextCompactionError("当前上下文没有可压缩的会话内容。")

    summary_messages = [message for group in groups for message in group]
    dropped_groups = 0
    single_drop_count = 0
    while summary_messages:
        request = ChatRequest(
            model=model,
            system=[SUMMARY_SYSTEM_PROMPT],
            messages=summary_messages,
            tools=[],
            allow_tool_calls=False,
            thinking=None,
            cancellation_token=cancellation_token,
        )
        if estimate_request_tokens(request, ContextManagementState()) >= context_window - SUMMARY_OUTPUT_RESERVE - EMERGENCY_MARGIN:
            groups, removed = _drop_oldest_groups(groups, single_drop_count)
            dropped_groups += removed
            single_drop_count += 1
            summary_messages = [message for group in groups for message in group]
            continue
        try:
            raw_summary = await _collect_summary(provider, request)
        except ProviderPromptTooLongError:
            groups, removed = _drop_oldest_groups(groups, single_drop_count)
            dropped_groups += removed
            single_drop_count += 1
            summary_messages = [message for group in groups for message in group]
            continue
        summary = parse_summary(raw_summary)
        recent = select_recent_history(transcript)
        recovery = build_recovery_prompt(state.recent_files, visible_tools)
        compacted = [
            ConversationMessage.text_message("user", "以下内容是较早会话的压缩历史。"),
            ConversationMessage.text_message("assistant", summary),
            ConversationMessage.text_message("user", recovery),
            *copy.deepcopy(recent),
        ]
        return TranscriptCompaction(transcript=compacted, dropped_groups=dropped_groups)
    raise ContextCompactionError("上下文过长，已无可用于摘要的完整消息组。")


def parse_summary(text: str) -> str:
    match = _SUMMARY_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ContextCompactionError("摘要响应必须只包含一个 <summary> 标签。")
    body = match.group("body").strip()
    if not body or "<summary>" in body or "</summary>" in body:
        raise ContextCompactionError("摘要标签为空或重复。")
    positions: list[int] = []
    for heading in SUMMARY_HEADINGS:
        marker = f"## {heading}"
        if body.count(marker) != 1:
            raise ContextCompactionError(f"摘要缺少或重复章节：{heading}")
        positions.append(body.index(marker))
    if positions != sorted(positions):
        raise ContextCompactionError("摘要章节顺序不正确。")
    return body


def group_complete_turns(transcript: list[ConversationMessage]) -> list[list[ConversationMessage]]:
    groups: list[list[ConversationMessage]] = []
    leading: list[ConversationMessage] = []
    for message in transcript:
        if message.role == "user":
            if not groups:
                groups.append([*leading, message])
                leading = []
            else:
                groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            leading.append(message)
    return groups


def select_recent_history(transcript: list[ConversationMessage]) -> list[ConversationMessage]:
    groups = group_complete_turns(transcript)
    selected: list[list[ConversationMessage]] = []
    message_count = 0
    token_count = 0
    for group in reversed(groups):
        selected.append(group)
        message_count += len(group)
        token_count += estimate_text_tokens(_canonical([asdict(message) for message in group]))
        if message_count >= RECENT_HISTORY_MESSAGES and token_count >= RECENT_HISTORY_TOKENS:
            break
    selected.reverse()
    return [message for group in selected for message in group]


def build_recovery_prompt(
    snapshots: list[ContextFileSnapshot],
    visible_tools: list[ToolDefinition],
) -> str:
    lines = ["继续工作前请使用以下恢复上下文。"]
    lines.append("\n## 最近读取的文件")
    if snapshots:
        for snapshot in snapshots[:RECENT_FILE_LIMIT]:
            lines.extend((f"### {snapshot.path}", f"读取时间：{snapshot.read_at}", snapshot.content))
    else:
        lines.append("暂无可靠文件快照。")
    lines.append("\n## 当前可见工具")
    if visible_tools:
        lines.extend(f"- {tool.name}: {tool.description}" for tool in visible_tools)
    else:
        lines.append("当前请求没有可调用工具。")
    lines.extend(
        (
            "\n## 边界提醒",
            "摘要和文件快照都可能被截断。需要文件、工具结果、错误或用户原话的精确内容时，必须重新调用工具读取，不得猜测。",
        )
    )
    return "\n".join(lines)


def record_file_snapshot(
    state: ContextManagementState,
    *,
    path: str,
    normalized_path: str,
    content: str,
) -> None:
    limit = int(RECENT_FILE_TOKENS * CHARACTERS_PER_TOKEN)
    truncated = content[:limit]
    if len(content) > limit:
        truncated += "\n(content truncated)"
    snapshot = ContextFileSnapshot(
        path=path,
        normalized_path=normalized_path,
        content=truncated,
        read_at=datetime.now(timezone.utc).isoformat(),
    )
    state.recent_files = [
        item for item in state.recent_files if item.normalized_path != normalized_path
    ]
    state.recent_files.insert(0, snapshot)
    del state.recent_files[RECENT_FILE_LIMIT:]


def automatic_threshold(context_window: int) -> int:
    return context_window - SUMMARY_OUTPUT_RESERVE - AUTOMATIC_MARGIN


async def _collect_summary(provider: ChatProvider, request: ChatRequest) -> str:
    parts: list[str] = []
    saw_tool_call = False
    async for event in provider.stream_chat(request):
        if event.kind == "text_delta" and event.text:
            parts.append(event.text)
        elif event.kind == "tool_call_delta":
            saw_tool_call = True
    if saw_tool_call:
        raise ContextCompactionError("摘要请求意外返回了工具调用。")
    return "".join(parts)


def _drop_oldest_groups(
    groups: list[list[ConversationMessage]],
    attempt: int,
) -> tuple[list[list[ConversationMessage]], int]:
    if len(groups) <= 1:
        return [], len(groups)
    count = 1 if attempt < 3 else max(1, math.ceil(len(groups) * 0.2))
    count = min(count, len(groups) - 1)
    return groups[count:], count


async def _write_tool_result(
    project_root: Path,
    context_id: str,
    call_id: str,
    text: str,
) -> ToolResultReplacement:
    safe_call_id = hashlib.sha256(call_id.encode("utf-8")).hexdigest() + ".txt"
    root = project_root.resolve()
    directory = (root / ".lancher" / "context" / context_id / "tool-results").resolve()
    if root not in directory.parents:
        raise OSError("工具结果目录越过项目边界。")
    path = directory / safe_call_id
    relative_path = path.relative_to(root).as_posix()
    preview = _build_tool_preview(text, relative_path)

    def write() -> None:
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    await asyncio.to_thread(write)
    return ToolResultReplacement(
        call_id=call_id,
        preview=preview,
        relative_path=relative_path,
        original_bytes=len(text.encode("utf-8")),
    )


def _build_tool_preview(text: str, relative_path: str) -> str:
    prefix = _utf8_prefix(text, TOOL_PREVIEW_BYTES)
    first_lines = "\n".join(prefix.splitlines()[:TOOL_PREVIEW_LINES])
    return (
        "[工具结果已卸载]\n"
        f"原始大小：{len(text.encode('utf-8'))} UTF-8 字节\n"
        f"完整内容：{relative_path}\n"
        "需要精确原文时请使用 read_file 重新读取。\n\n"
        f"预览：\n{first_lines}"
    )


def _utf8_prefix(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _serialize_request(request: ChatRequest) -> tuple[str, str, str]:
    system_tools = {
        "system": request.system,
        "tools": [asdict(tool) for tool in request.tools],
        "allow_tool_calls": request.allow_tool_calls,
        "thinking": asdict(request.thinking) if request.thinking else None,
        "mode": request.mode,
    }
    message_data = [asdict(message) for message in request.messages]
    shape = _canonical(system_tools)
    messages = _canonical(message_data)
    return _canonical({"shape": system_tools, "messages": message_data}), _digest(shape), _digest(messages)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _without_dynamic_reminders(
    transcript: list[ConversationMessage],
) -> list[ConversationMessage]:
    cleaned: list[ConversationMessage] = []
    for message in copy.deepcopy(transcript):
        if (
            message.role == "user"
            and len(message.blocks) > 1
            and message.blocks[0].kind == "text"
            and message.blocks[0].text.startswith("<system-reminder>\n")
        ):
            message.blocks = message.blocks[1:]
        cleaned.append(message)
    return cleaned
