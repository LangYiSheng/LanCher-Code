from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from lancher_code.context.budget import context_budget
from lancher_code.context.models import ContextManagementState
from lancher_code.context.tokens import estimate_text_tokens, text_prefix_for_tokens
from lancher_code.contracts.messages import ContentBlock, ConversationMessage
from lancher_code.logging_system import get_logger
from lancher_code.sessions.paths import validate_control_path


logger = get_logger("context.offload")
TOOL_PREVIEW_LINES = 20
TOOL_PREVIEW_BYTES = 2048


def _tool_preview_token_budget(context_window: int, result_count: int) -> int:
    budget = context_budget(context_window)
    # 额度包含预览包装和路径；正文只使用扣除包装之后的剩余空间。
    return min(512, max(1, budget.tool_result_tokens // 4),
               max(1, budget.tool_batch_tokens // max(1, result_count)))


def project_tool_results(
    transcript: list[ConversationMessage], state: ContextManagementState,
    *, context_window: int,
) -> list[ConversationMessage]:
    """完整历史保留原文，只有送往模型的视图应用落盘预览。"""
    candidate = copy.deepcopy(transcript)
    result_count = sum(block.kind == "tool_result" for message in candidate for block in message.blocks)
    preview_tokens = _tool_preview_token_budget(context_window, result_count)
    for message in candidate:
        for block in message.blocks:
            replacement = state.replacements.get(block.call_id)
            if block.kind != 'tool_result':
                continue
            key = block.call_id + ':' + hashlib.sha256(block.text.encode('utf-8')).hexdigest()
            if key not in state.frozen_tool_previews:
                state.frozen_tool_previews[key] = (_build_tool_preview(block.text, replacement, token_budget=preview_tokens)
                                                  if replacement is not None else None)
            frozen = state.frozen_tool_previews[key]
            if frozen is not None:
                block.text = frozen
    return candidate


async def offload_tool_results(
    transcript: list[ConversationMessage],
    state: ContextManagementState,
    project_root: Path,
    *,
    result_directory: Path,
    context_window: int,
) -> int:
    result_blocks: dict[str, ContentBlock] = {}
    result_order: dict[str, int] = {}
    batches: list[list[str]] = []
    call_to_batch: dict[str, int] = {}

    for message in transcript:
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
    token_sizes = {call_id: estimate_text_tokens(result_blocks[call_id].text) for call_id in new_ids}
    budget = context_budget(context_window)
    single_limit = budget.tool_result_tokens
    batch_limit = budget.tool_batch_tokens
    selected = {call_id for call_id in new_ids if token_sizes[call_id] > single_limit}
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
        relative_path = replacement if replacement is not None else (
            directory / (hashlib.sha256(call_id.encode("utf-8")).hexdigest() + ".txt")
        ).relative_to(root).as_posix()
        frozen_key = call_id + ':' + hashlib.sha256(block.text.encode('utf-8')).hexdigest()
        preview = state.frozen_tool_previews.get(frozen_key)
        if frozen_key in state.frozen_tool_previews:
            preview_costs[call_id] = estimate_text_tokens(block.text if preview is None else preview)
        else:
            preview_costs[call_id] = estimate_text_tokens(_build_tool_preview(block.text, relative_path, token_budget=preview_tokens))

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
            continue
        try:
            replacement = await _write_tool_result(
                project_root, result_directory, call_id, block.text,
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
        offloaded_count += 1

    if offloaded_count:
        logger.info(
            "event=tool_results_offloaded context_id=%s count=%s",
            state.context_id,
            offloaded_count,
        )
    return offloaded_count


async def _write_tool_result(
    project_root: Path,
    result_directory: Path,
    call_id: str,
    text: str,
) -> str:
    safe_call_id = hashlib.sha256(call_id.encode("utf-8")).hexdigest() + ".txt"
    root = project_root.resolve()
    directory = result_directory.absolute()
    validate_control_path(root, directory)
    if root not in directory.parents:
        raise OSError("工具结果目录越过项目边界。")
    path = directory / safe_call_id
    validate_control_path(root, path)
    relative_path = path.relative_to(root).as_posix()

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
    return relative_path


def _build_tool_preview(text: str, relative_path: str, *, token_budget: int) -> str:
    original_bytes = len(text.encode("utf-8"))
    header = (
        "[工具结果已卸载]\n"
        f"原始大小：{original_bytes} UTF-8 字节\n"
        f"完整内容：{relative_path}\n"
        "需要精确原文时请使用 read_file 重新读取。\n\n"
    )
    middle = "\n\n[中间内容请读取原文]\n\n末尾预览：\n"
    wrapper = header + "开头预览：\n" + middle
    if estimate_text_tokens(wrapper) > token_budget:
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
    available = max(0, token_budget - estimate_text_tokens(wrapper))
    first_lines = text_prefix_for_tokens(first_lines, available // 2)
    # 末尾往往带退出状态或错误。按相同分类规则取后缀，避免仅留下开头。
    last_lines = text_prefix_for_tokens(last_lines[::-1], available - available // 2)[::-1]
    return header + "开头预览：\n" + first_lines + middle + last_lines


def _utf8_prefix(text: str, byte_limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")
