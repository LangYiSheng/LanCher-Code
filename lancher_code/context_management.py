from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from lancher_code.errors import ContextCompactionError, ProviderPromptTooLongError
from lancher_code.context_budget import context_budget
from lancher_code.context_tokens import (
    estimate_messages_tokens,
    estimate_request,
    estimate_request_tokens,
    estimate_text_tokens,
    update_usage_anchor,
)
from lancher_code.logging_system import get_logger
from lancher_code.models import (
    CancellationToken,
    ChatRequest,
    ContentBlock,
    ContextFileSnapshot,
    ContextManagementState,
    ConversationMessage,
    MessageUsage,
    ToolDefinition,
    ToolResultReplacement,
)
from lancher_code.providers.base import ChatProvider
from lancher_code.sessions.paths import validate_control_path


logger = get_logger("context_management")

SINGLE_TOOL_RESULT_BYTES = 50_000
TOOL_BATCH_BYTES = 200_000
TOOL_PREVIEW_LINES = 20
TOOL_PREVIEW_BYTES = 2_048
RECENT_FILE_LIMIT = 5
RECENT_FILE_TOKENS = 5_000
AUTOMATIC_FAILURE_LIMIT = 3

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


def _tool_preview_token_budget(context_window: int, result_count: int) -> int:
    budget = context_budget(context_window)
    # 额度包含预览包装和路径；正文只使用扣除包装之后的剩余空间。
    return min(512, max(1, budget.tool_result_tokens // 4),
               max(1, budget.tool_batch_tokens // max(1, result_count)))


def project_tool_results(
    transcript: list[ConversationMessage], state: ContextManagementState,
    *, context_window: int | None = None,
) -> list[ConversationMessage]:
    """完整历史保留原文，只有送往模型的视图应用落盘预览。"""
    candidate = copy.deepcopy(transcript)
    budget = context_budget(context_window) if context_window is not None else None
    result_count = sum(block.kind == "tool_result" for message in candidate for block in message.blocks)
    preview_tokens = _tool_preview_token_budget(context_window, result_count) if budget else None
    for message in candidate:
        for block in message.blocks:
            replacement = state.replacements.get(block.call_id)
            if block.kind == "tool_result" and replacement is not None:
                block.text = (
                    _build_tool_preview(block.text, replacement.relative_path, token_budget=preview_tokens)
                    if budget is not None else replacement.preview
                )
    return candidate


async def offload_tool_results(
    transcript: list[ConversationMessage],
    state: ContextManagementState,
    project_root: Path,
    *,
    result_directory: Path,
    context_window: int | None = None,
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

    # 切换为较小窗口后，之前未超过限制的结果也必须重新检查。
    new_ids = [call_id for call_id in result_blocks if call_id not in state.replacements]
    byte_sizes = {call_id: len(result_blocks[call_id].text.encode("utf-8")) for call_id in new_ids}
    token_sizes = {call_id: estimate_text_tokens(result_blocks[call_id].text) for call_id in new_ids}
    budget = context_budget(context_window) if context_window is not None else None
    sizes = token_sizes if budget is not None else byte_sizes
    single_limit = budget.tool_result_tokens if budget else SINGLE_TOOL_RESULT_BYTES
    batch_limit = budget.tool_batch_tokens if budget else TOOL_BATCH_BYTES
    selected = {call_id for call_id in new_ids if sizes[call_id] > single_limit}
    preview_tokens = None
    if budget is None:
        for batch in batches:
            remaining = [call_id for call_id in batch if call_id in byte_sizes and call_id not in selected]
            total = sum(sizes[call_id] for call_id in remaining)
            for call_id in sorted(remaining, key=lambda item: (-sizes[item], result_order[item])):
                if total <= batch_limit:
                    break
                selected.add(call_id)
                total -= sizes[call_id]
    else:
        preview_tokens = _tool_preview_token_budget(context_window, len(result_blocks))
        raw_costs = {call_id: estimate_text_tokens(block.text) for call_id, block in result_blocks.items()}
        preview_costs: dict[str, int] = {}
        root = project_root.resolve()
        directory = result_directory.absolute()
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise OSError("工具结果目录越过项目边界。") from exc
        for call_id, block in result_blocks.items():
            replacement = state.replacements.get(call_id)
            relative_path = replacement.relative_path if replacement is not None else (
                directory / (hashlib.sha256(call_id.encode("utf-8")).hexdigest() + ".txt")
            ).relative_to(root).as_posix()
            preview_costs[call_id] = estimate_text_tokens(
                _build_tool_preview(block.text, relative_path, token_budget=preview_tokens)
            )

        def current_cost(call_id: str) -> int:
            return preview_costs[call_id] if call_id in selected or call_id in state.replacements else raw_costs[call_id]

        def fit_group(call_ids: list[str]) -> None:
            present = list(dict.fromkeys(call_id for call_id in call_ids if call_id in result_blocks))
            total = sum(current_cost(call_id) for call_id in present)
            remaining = [call_id for call_id in present if call_id in new_ids and call_id not in selected]
            # 选择最大的实际节省量，而不是假定落盘后的结果占零空间。
            for call_id in sorted(remaining, key=lambda item: (-(raw_costs[item] - preview_costs[item]), result_order[item])):
                if total <= batch_limit:
                    break
                saving = raw_costs[call_id] - preview_costs[call_id]
                if saving <= 0:
                    continue
                selected.add(call_id)
                total -= saving

        for batch in batches:
            fit_group(batch)
        # 已存在的预览与多轮结果都参与全局预算；元信息自身过大时只能
        # 保留调用配对，由完整请求的硬预算检查决定是否进一步压缩。
        fit_group(list(result_blocks))

    offloaded_count = 0
    for call_id in new_ids:
        block = result_blocks[call_id]
        if call_id not in call_to_batch:
            logger.warning("event=orphan_tool_result context_id=%s call_id=%s", state.context_id, call_id)
        if call_id not in selected:
            state.seen_call_ids.add(call_id)
            continue
        try:
            replacement = await _write_tool_result(
                project_root, result_directory, call_id, block.text,
                preview_tokens=preview_tokens,
            )
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
    return ToolOffloadResult(
        transcript=project_tool_results(transcript, state, context_window=context_window),
        offloaded_count=offloaded_count,
    )


async def compact_transcript(
    *,
    provider: ChatProvider,
    model: str,
    transcript: list[ConversationMessage],
    visible_tools: list[ToolDefinition],
    state: ContextManagementState,
    context_window: int,
    cancellation_token: CancellationToken | None = None,
    max_output_tokens: int | None = None,
    request_factory: Callable[[ChatRequest], ChatRequest] | None = None,
) -> TranscriptCompaction:
    transcript = _without_dynamic_reminders(transcript)
    pending_call_ids: set[str] = set()
    for message in transcript:
        for block in message.blocks:
            if block.kind == "tool_use":
                pending_call_ids.add(block.call_id)
            elif block.kind == "tool_result":
                pending_call_ids.discard(block.call_id)
    if pending_call_ids:
        raise ContextCompactionError("存在未完成的工具调用，不能安全压缩上下文。")
    budget = context_budget(context_window, max_output_tokens, purpose="compaction")
    before_tokens = estimate_messages_tokens(transcript)
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
            max_output_tokens=budget.output_tokens,
            purpose="compaction",
        )
        if estimate_request_tokens(request, ContextManagementState()) > budget.input_limit:
            groups, removed = _drop_oldest_groups(groups, single_drop_count)
            dropped_groups += removed
            single_drop_count += 1
            summary_messages = [message for group in groups for message in group]
            continue
        try:
            if request_factory is not None:
                request = request_factory(request)
            raw_summary = await _collect_summary(provider, request)
        except ProviderPromptTooLongError:
            groups, removed = _drop_oldest_groups(groups, single_drop_count)
            dropped_groups += removed
            single_drop_count += 1
            summary_messages = [message for group in groups for message in group]
            continue
        summary = parse_summary(raw_summary)
        recent = select_recent_history(transcript, token_budget=budget.recent_history_tokens)
        latest_user = next((message for message in reversed(transcript) if message.role == "user"), None)
        if latest_user is not None and not any(message is latest_user for message in recent):
            # 大工具交换可以整组交给摘要，但当前用户原话仍是后续工作的
            # 直接约束。只补回用户消息，不带孤立的工具调用或结果。
            recent = [latest_user, *recent]
        recovery = build_recovery_prompt(
            state.recent_files, visible_tools,
            token_budget=max(128, budget.recent_history_tokens // 2),
        )
        compacted = [
            ConversationMessage.text_message("user", "以下内容是较早会话的压缩历史。"),
            ConversationMessage.text_message("assistant", summary),
            ConversationMessage.text_message("user", recovery),
            *copy.deepcopy(recent),
        ]
        after_tokens = estimate_messages_tokens(compacted)
        if after_tokens >= before_tokens:
            raise ContextCompactionError("摘要没有缩小上下文，保留原有历史。")
        if after_tokens > context_budget(context_window).input_limit:
            raise ContextCompactionError("摘要与恢复内容仍超过可用输入预算，保留原有历史。")
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
    pending_call_ids: set[str] = set()
    for message in transcript:
        if message.role == "user" and not pending_call_ids:
            if not groups:
                groups.append([*leading, message])
                leading = []
            else:
                groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            leading.append(message)
        for block in message.blocks:
            if block.kind == "tool_use" and block.call_id:
                pending_call_ids.add(block.call_id)
            elif block.kind == "tool_result":
                pending_call_ids.discard(block.call_id)
    if not groups and leading:
        groups.append(leading)
    return groups


def select_recent_history(
    transcript: list[ConversationMessage], *, token_budget: int | None = None,
) -> list[ConversationMessage]:
    groups = group_complete_turns(transcript)
    selected: list[list[ConversationMessage]] = []
    token_count = 0
    for group in reversed(groups):
        group_tokens = estimate_messages_tokens(group)
        if token_budget is not None and token_count + group_tokens > token_budget:
            break
        selected.append(group)
        token_count += group_tokens
        if token_budget is None and len([message for item in selected for message in item]) >= 5 and token_count >= 10_000:
            break
    selected.reverse()
    return [message for group in selected for message in group]


def build_recovery_prompt(
    snapshots: list[ContextFileSnapshot],
    visible_tools: list[ToolDefinition],
    *,
    token_budget: int | None = None,
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
    content = "\n".join(lines)
    return _truncate_text_tokens(content, token_budget) if token_budget is not None else content


def record_file_snapshot(
    state: ContextManagementState,
    *,
    path: str,
    normalized_path: str,
    content: str,
) -> None:
    truncated = _truncate_text_tokens(content, RECENT_FILE_TOKENS)
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


def automatic_threshold(context_window: int, max_output_tokens: int | None = None) -> int:
    return context_budget(context_window, max_output_tokens).automatic_threshold


async def _collect_summary(provider: ChatProvider, request: ChatRequest) -> str:
    parts: list[str] = []
    saw_tool_call = False
    completed = False
    usage: MessageUsage | None = None
    stop_reason: str | None = None
    async for event in provider.stream_chat(request):
        if event.kind == "text_delta" and event.text:
            parts.append(event.text)
        elif event.kind == "tool_call_delta":
            saw_tool_call = True
        elif event.kind == "message_end":
            completed = True
            usage = event.usage
            stop_reason = getattr(event, "stop_reason", None)
    if saw_tool_call:
        raise ContextCompactionError("摘要请求意外返回了工具调用。")
    if not completed:
        raise ContextCompactionError("摘要响应未正常结束，保留原有历史。")
    if stop_reason in {"length", "max_tokens", "max_output_tokens"} or (
        usage is not None and usage.output_tokens is not None
        and request.max_output_tokens is not None and usage.output_tokens >= request.max_output_tokens
    ):
        raise ContextCompactionError("摘要达到输出上限，可能不完整，保留原有历史。")
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
    result_directory: Path,
    call_id: str,
    text: str,
    *,
    preview_tokens: int | None = None,
) -> ToolResultReplacement:
    safe_call_id = hashlib.sha256(call_id.encode("utf-8")).hexdigest() + ".txt"
    root = project_root.resolve()
    directory = result_directory.absolute()
    validate_control_path(root, directory)
    if root not in directory.parents:
        raise OSError("工具结果目录越过项目边界。")
    path = directory / safe_call_id
    validate_control_path(root, path)
    relative_path = path.relative_to(root).as_posix()
    preview = _build_tool_preview(text, relative_path, token_budget=preview_tokens)

    def write() -> None:
        validate_control_path(root, directory)
        validate_control_path(root, path)
        directory.mkdir(parents=True, exist_ok=True)
        validate_control_path(root, directory)
        if path.exists():
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        validate_control_path(root, temporary)
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            validate_control_path(root, path)
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


def _build_tool_preview(text: str, relative_path: str, *, token_budget: int | None = None) -> str:
    original_bytes = len(text.encode("utf-8"))
    header = (
        "[工具结果已卸载]\n"
        f"原始大小：{original_bytes} UTF-8 字节\n"
        f"完整内容：{relative_path}\n"
        "需要精确原文时请使用 read_file 重新读取。\n\n"
    )
    middle = "\n\n[中间内容请读取原文]\n\n末尾预览：\n"
    wrapper = header + "开头预览：\n" + middle
    if token_budget is not None and estimate_text_tokens(wrapper) > token_budget:
        # 路径和来源信息不可伪造或裁掉。大量结果使元信息本身超额度时，
        # 留最小引用；完整请求的硬检查仍会发现不可容纳的输入。
        return (
            "[工具结果已卸载]\n"
            f"原始大小：{original_bytes} UTF-8 字节\n"
            f"完整内容：{relative_path}\n"
            "需要原文请使用 read_file。"
        )
    prefix = _utf8_prefix(text, TOOL_PREVIEW_BYTES)
    first_lines = "\n".join(prefix.splitlines()[:TOOL_PREVIEW_LINES])
    suffix = text.encode("utf-8")[-TOOL_PREVIEW_BYTES:].decode("utf-8", errors="ignore")
    last_lines = "\n".join(suffix.splitlines()[-TOOL_PREVIEW_LINES:])
    if token_budget is not None:
        available = max(0, token_budget - estimate_text_tokens(wrapper))
        first_lines = _text_prefix_for_tokens(first_lines, available // 2)
        # 末尾往往带退出状态或错误。按相同分类规则取后缀，避免仅留下开头。
        last_lines = _text_prefix_for_tokens(last_lines[::-1], available - available // 2)[::-1]
    return header + "开头预览：\n" + first_lines + middle + last_lines


def _utf8_prefix(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _truncate_text_tokens(text: str, token_budget: int) -> str:
    if estimate_text_tokens(text) <= token_budget:
        return text
    marker = "\n[内容已截断，请重新读取原文]"
    available = max(0, token_budget - estimate_text_tokens(marker))
    return _text_prefix_for_tokens(text, available) + marker


def _text_prefix_for_tokens(text: str, token_budget: int) -> str:
    if estimate_text_tokens(text) <= token_budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


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
