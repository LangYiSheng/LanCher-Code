from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from uuid import uuid4
from functools import wraps
from time import monotonic

from lancher_code.models import (
    ChatRequest,
    ContentBlock,
    ContextCompactionResult,
    ContextManagementState,
    ConversationMessage,
    DeferredToolGroup,
    MessageUsage,
    ProviderConfig,
    PromptContext,
    RuntimeMode,
    WorkPhase,
    PermissionPolicy,
    PlanSnapshot,
    PendingInput,
    SessionMessage,
    SessionState,
    ThinkingConfig,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    TraceEntry,
    legacy_runtime_mode,
    resolve_runtime_axes,
    tool_available_in_phase,
)
from lancher_code.context_management import (
    compact_transcript,
    estimate_request_tokens,
    offload_tool_results,
    record_file_snapshot,
    update_usage_anchor,
)
from lancher_code.logging_system import get_logger
from lancher_code.providers.base import ChatProvider

from lancher_code.permission_engine import PermissionStorage
from lancher_code.prompting import (
    build_chat_request_payload,
    build_dynamic_context_prompt,
    build_prompt_context,
    build_user_message,
)
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.service import SessionService
from lancher_code.sessions.repository import SessionRepositoryError


logger = get_logger("session")


def persist_change(*, streaming=False):
    """统一变更出口：工具与状态立即保存，流式文字按时间窗口合并。"""
    def decorate(method):
        @wraps(method)
        def changed(self, *args, **kwargs):
            result = method(self, *args, **kwargs)
            self.flush(force=not streaming)
            return result
        return changed
    return decorate



class SessionController:
    """管理当前进程内的会话状态与协议无关 transcript。"""

    def __init__(
        self,
        provider_config: ProviderConfig,
        state: SessionState | None = None,
        *,
        cwd: Path | None = None,
        current_date: date | None = None,
        initial_runtime_mode: RuntimeMode | None = None,
        initial_work_phase: WorkPhase | None = None,
        initial_permission_policy: PermissionPolicy | None = None,
        permission_storage: PermissionStorage | None = None,
        selected_model_ref: str | None = None,
    ) -> None:
        self._provider_config = provider_config
        self._selected_model_ref = selected_model_ref
        self._state = state or SessionState()
        self._cwd = (cwd or Path.cwd()).resolve()
        self._current_date = current_date or datetime.now().astimezone().date()
        self._transcript: list[ConversationMessage] = []
        self._sessions = SessionService(self._cwd)
        self._last_flush = monotonic()
        self._dirty = False
        self._context_lock = asyncio.Lock()
        self._active_dynamic_context: str | None = None
        self._permission_storage = permission_storage or PermissionStorage()
        self._permission_storage.subscribe_session_rules_changed(self._permissions_changed)
        if any(value is not None for value in (initial_runtime_mode, initial_work_phase, initial_permission_policy)):
            phase, policy = resolve_runtime_axes(initial_runtime_mode, initial_work_phase, initial_permission_policy)
            self.set_permission_policy(policy)
            self.set_work_phase(phase)
        self._initial_phase = self.work_phase
        self._initial_policy = self.permission_policy

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def transcript(self) -> list[ConversationMessage]:
        return list(self._transcript)

    @property
    def runtime_mode(self) -> RuntimeMode:
        return self._state.runtime_mode

    @property
    def work_phase(self) -> WorkPhase:
        return self._state.work_phase

    @property
    def permission_policy(self) -> PermissionPolicy:
        return self._state.permission_policy

    @property
    def session_id(self) -> str | None:
        return self._state.session_id

    @property
    def plan_snapshot(self) -> PlanSnapshot | None:
        return copy.deepcopy(self._state.plan_snapshot)

    @property
    def pending_inputs(self) -> list[PendingInput]:
        return copy.deepcopy(self._state.pending_inputs)

    @persist_change()
    def set_plan_snapshot(
        self, snapshot: PlanSnapshot | str | None, *, source_message_id: str = "", ready: bool = False
    ) -> PlanSnapshot | None:
        if isinstance(snapshot, str):
            snapshot = PlanSnapshot.create(snapshot, source_message_id, ready=ready)
        if snapshot is not None:
            snapshot = SessionCodec._decode_plan_snapshot(asdict(snapshot))
        self._state.plan_snapshot = copy.deepcopy(snapshot)
        self._mark_dirty()
        return self.plan_snapshot

    @persist_change()
    def update_pending_inputs(self, items: list[PendingInput]) -> None:
        self._state.pending_inputs = SessionCodec._decode_pending_inputs([asdict(item) for item in items], restore=False)
        self._mark_dirty()

    @property
    def paths(self):
        return self._sessions.paths

    @property
    def plan_file_path(self) -> Path | None:
        return self.paths.plan if self.paths is not None else None

    @property
    def session_title(self) -> str | None:
        return self._sessions.title

    @property
    def context_state(self) -> ContextManagementState:
        return self._state.context_management

    @property
    def context_window(self) -> int:
        return self._provider_config.context_window

    @property
    def provider_config(self) -> ProviderConfig:
        return self._provider_config

    @property
    def selected_model_ref(self) -> str | None:
        return self._selected_model_ref

    def set_model(self, config: ProviderConfig, model_ref: str, *, initial: bool = False) -> None:
        """保留会话内容，原子更新模型与依赖模型的上下文校准。"""
        previous_config = self._provider_config
        previous_ref = self._selected_model_ref
        previous_context = copy.deepcopy(self.context_state)
        previous_dirty = self._dirty
        self._provider_config = config
        self._selected_model_ref = model_ref
        self._reset_model_context(self.context_state)
        if initial:
            return
        self._mark_dirty()
        if self._sessions.writer is not None:
            try:
                self.flush()
            except Exception:
                self._provider_config = previous_config
                self._selected_model_ref = previous_ref
                self._state.context_management = previous_context
                self._dirty = previous_dirty
                raise

    @staticmethod
    def _reset_model_context(context: ContextManagementState) -> None:
        context.usage_anchor = None
        context.automatic_failure_count = 0
        context.automatic_compaction_disabled = False

    def set_runtime_mode(self, mode: RuntimeMode) -> RuntimeMode:
        """兼容旧显式调用；新业务只能分别设置阶段和权限。"""
        phase, policy = resolve_runtime_axes(mode)
        if mode != "plan":
            self.set_permission_policy(policy)
        self.set_work_phase(phase)
        return self.runtime_mode

    @persist_change()
    def set_work_phase(self, phase: WorkPhase) -> WorkPhase:
        resolve_runtime_axes(work_phase=phase, permission_policy=self.permission_policy)
        previous_phase = self.work_phase
        if phase == previous_phase:
            return phase
        self._state.previous_runtime_mode = self.runtime_mode
        if phase == "plan":
            self._state.pending_plan_entry_kind = "reentry" if self._state.plan_snapshot else "initial"
            self._state.pending_plan_exit_notice = False
            self._state.plan_mode_turn_count = 0
        elif previous_phase == "plan":
            self._state.pending_plan_exit_notice = self._state.plan_mode_turn_count > 0
            self._state.pending_plan_entry_kind = None
        self._state.work_phase = phase
        self._mark_dirty()
        return phase

    @persist_change()
    def set_permission_policy(self, policy: PermissionPolicy) -> PermissionPolicy:
        resolve_runtime_axes(work_phase=self.work_phase, permission_policy=policy)
        if policy != self.permission_policy:
            self._state.permission_policy = policy
            self._mark_dirty()
        return policy

    def restore_mode_after_plan(self) -> RuntimeMode:
        self.set_work_phase("execute")
        return self.runtime_mode

    @persist_change()
    def create_user_message(self, text: str) -> SessionMessage:
        if not text.strip():
            raise ValueError('用户消息不能为空。')
        if self.session_id is None:
            self._state.session_id = self._sessions.create(self._snapshot(), text)
        if self.work_phase == "plan" and self._state.plan_snapshot is not None:
            self._state.plan_snapshot.ready = False
        message = SessionMessage(
            id=self._new_message_id(),
            role="user",
            content=text,
            status="complete",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        self._active_dynamic_context = build_dynamic_context_prompt(self._prompt_context())
        self._transcript.append(
            build_user_message(text=text, dynamic_context=self._active_dynamic_context)
        )
        self._advance_dynamic_prompt_state_after_user_turn()
        self._mark_dirty()
        return message

    @persist_change()
    def create_assistant_message(self) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="assistant",
            content="",
            status="streaming",
            timestamp=self._now(),
            timeline_version=1,
        )
        self._state.messages.append(message)
        self._mark_dirty()
        return message

    @persist_change(streaming=True)
    def append_message_content(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content += delta
        if message.timeline_version == 1:
            self.append_trace_text(message_id, delta)
        return message

    @persist_change()
    def clear_message_content(self, message_id: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content = ""
        return message

    @persist_change()
    def add_message_usage(self, message_id: str, usage: MessageUsage) -> SessionMessage:
        message = self.get_message(message_id)
        message.usage.input_tokens += usage.input_tokens
        message.usage.cached_input_tokens += usage.cached_input_tokens
        message.usage.output_tokens += usage.output_tokens
        return message

    @persist_change(streaming=True)
    def append_trace_thinking(self, message_id: str, delta: str) -> SessionMessage:
        return self._append_stream_segment(message_id, "thinking", delta)

    @persist_change(streaming=True)
    def append_trace_text(self, message_id: str, text: str) -> SessionMessage:
        return self._append_stream_segment(message_id, "text", text)

    def _append_stream_segment(self, message_id: str, kind: Literal["text", "thinking"], text: str) -> SessionMessage:
        message = self.get_message(message_id)
        if text:
            self._expand_trace_on_first_entry(message)
            entries = message.trace.entries
            if entries and entries[-1].kind == kind and entries[-1].metadata.get("state") == "streaming":
                entries[-1].text += text
            else:
                self.finish_trace_segment(message_id)
                entries.append(TraceEntry(kind=kind, text=text, metadata={"state": "streaming"}))
        return message

    @persist_change(streaming=True)
    def finish_trace_segment(self, message_id: str, state: str = "complete") -> SessionMessage:
        """封口当前输出段，下一条同类输出也不会跨响应合并。"""
        message = self.get_message(message_id)
        if message.trace.entries:
            entry = message.trace.entries[-1]
            if entry.kind in {"thinking", "text"} and entry.metadata.get("state") == "streaming":
                entry.metadata["state"] = state
        return message

    @persist_change()
    def append_trace_notice(self, message_id: str, text: str) -> SessionMessage:
        message = self.finish_trace_segment(message_id)
        self._expand_trace_on_first_entry(message)
        message.trace.entries.append(TraceEntry(kind="notice", text=text))
        return message

    @persist_change()
    def append_trace_tool_calls(self, message_id: str, tool_calls: list[ToolCall]) -> SessionMessage:
        message = self.finish_trace_segment(message_id)
        self._expand_trace_on_first_entry(message)
        group_id = uuid4().hex
        for call in tool_calls:
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_call",
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    metadata={"group_id": group_id, "state": "queued", "started": False},
                )
            )
        return message

    @persist_change()
    def set_trace_tool_state(self, message_id: str, call_id: str, state: str) -> SessionMessage:
        message = self.get_message(message_id)
        for entry in reversed(message.trace.entries):
            if entry.kind == "tool_call" and entry.call_id == call_id:
                entry.metadata["state"] = state
                if state == "running":
                    entry.metadata["started"] = True
                break
        return message

    @persist_change()
    def append_trace_tool_results(self, message_id: str, results: list[ToolExecutionResult]) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        for result in results:
            call_entry = next((entry for entry in reversed(message.trace.entries)
                               if entry.kind == "tool_call" and entry.call_id == result.call_id), None)
            state = "complete" if result.ok else "error"
            if result.error_code == "steering_superseded":
                state = "skipped"
            elif result.error_code == "tool_result_interrupted":
                state = "cancelled"
            metadata = {**result.metadata, "content": result.content, "state": state,
                        "error_code": result.error_code, "error_message": result.error_message}
            if call_entry is not None:
                call_entry.metadata["state"] = state
                metadata["group_id"] = call_entry.metadata.get("group_id")
                metadata["started"] = call_entry.metadata.get("started", False)
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_result",
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    text=result.summary if result.ok else (result.error_message or result.summary),
                    metadata=metadata,
                    ok=result.ok,
                )
            )
        return message

    @persist_change()
    def append_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        if not tool_calls:
            return

        blocks = [
            ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name, input=call.arguments)
            for call in tool_calls
        ]
        self._transcript.append(ConversationMessage(role="assistant", blocks=blocks))

    @persist_change()
    def append_tool_results(self, results: list[ToolExecutionResult]) -> None:
        if not results:
            return

        blocks = [
            ContentBlock.tool_result_block(
                call_id=result.call_id,
                text=self._tool_result_content(result),
                is_error=result.is_error,
            )
            for result in results
        ]
        # 实时结果仍归入同一批协议消息，保存/恢复时不会拆散调用配对。
        if self._transcript and self._transcript[-1].role == "tool":
            self._transcript[-1].blocks.extend(blocks)
        else:
            self._transcript.append(ConversationMessage(role="tool", blocks=blocks))

    @persist_change()
    def complete_message(self, message_id: str, usage: MessageUsage | None = None) -> SessionMessage:
        message = self.finish_trace_segment(message_id)
        message.status = "complete"
        message.trace.collapsed = True
        message.usage = usage or MessageUsage()
        if message.content.strip():
            self._transcript.append(ConversationMessage.text_message("assistant", message.content))
        self._active_dynamic_context = None
        return message

    @persist_change()
    def fail_message(self, message_id: str, error_text: str) -> SessionMessage:
        message = self.finish_trace_segment(message_id, "error")
        if message.timeline_version == 1 and not self._has_last_notice(message, error_text):
            self.append_trace_notice(message_id, error_text)
        message.status = "error"
        message.content = error_text
        message.trace.collapsed = True
        self._active_dynamic_context = None
        return message

    @persist_change()
    def cancel_message(self, message_id: str, notice_text: str = "本轮已取消。") -> SessionMessage:
        message = self.finish_trace_segment(message_id, "cancelled")
        if message.timeline_version == 1 and not self._has_last_notice(message, notice_text):
            self.append_trace_notice(message_id, notice_text)
        message.status = "cancelled"
        if not message.content.strip():
            message.content = notice_text
        message.trace.collapsed = True
        self._active_dynamic_context = None
        return message

    @staticmethod
    def _has_last_notice(message: SessionMessage, text: str) -> bool:
        return bool(message.trace.entries and message.trace.entries[-1].kind == "notice"
                    and message.trace.entries[-1].text == text)

    def get_message(self, message_id: str) -> SessionMessage:
        for message in self._state.messages:
            if message.id == message_id:
                return message
        raise KeyError(f"未找到消息：{message_id}")

    def build_request(
        self,
        tools: list[ToolDefinition],
        *,
        allow_tool_calls: bool,
        mode: RuntimeMode | None = None,
        work_phase: WorkPhase | None = None,
        permission_policy: PermissionPolicy | None = None,
        deferred_tool_groups: list[DeferredToolGroup] | None = None,
    ) -> ChatRequest:
        active_phase, active_policy = resolve_runtime_axes(
            mode,
            work_phase if work_phase is not None else (None if mode is not None else self.work_phase),
            permission_policy if permission_policy is not None else (None if mode is not None else self.permission_policy),
        )
        thinking = self._request_thinking()
        if not allow_tool_calls:
            filtered_tools = []
        else:
            filtered_tools = [tool for tool in tools if tool_available_in_phase(tool, active_phase)]
        payload = build_chat_request_payload(
            context=self._prompt_context(work_phase=active_phase, permission_policy=active_policy),
            transcript=self._request_transcript(),
            tools=filtered_tools,
            deferred_tool_groups=deferred_tool_groups,
            dynamic_context=None,
        )
        return ChatRequest(
            model=self._provider_config.model,
            system=payload.system,
            messages=payload.messages,
            tools=payload.tools,
            allow_tool_calls=allow_tool_calls,
            thinking=thinking,
            mode=legacy_runtime_mode(active_phase, active_policy),
            work_phase=active_phase,
            permission_policy=active_policy,
        )

    def estimate_request_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request, self.context_state)

    @persist_change()
    def update_context_usage(self, request: ChatRequest, usage: MessageUsage) -> None:
        if usage.input_tokens + usage.output_tokens > 0:
            update_usage_anchor(self.context_state, request, usage)
        else:
            self.context_state.usage_anchor = None
        self._mark_dirty()

    @persist_change()
    def record_read_file_result(self, result: ToolExecutionResult) -> None:
        if result.is_error or result.tool_name != "read_file":
            return
        path = result.metadata.get("normalized_path")
        relative_path = result.metadata.get("relative_path")
        content = result.metadata.get("source_content")
        if (
            not isinstance(path, str)
            or not isinstance(relative_path, str)
            or not isinstance(content, str)
        ):
            return
        record_file_snapshot(
            self.context_state,
            path=relative_path,
            normalized_path=path,
            content=content,
        )
        self._mark_dirty()

    async def offload_large_tool_results(self) -> int:
        async with self._context_lock:
            working_state = copy.deepcopy(self.context_state)
            if self.paths is None:
                return 0
            self.paths.validate()
            result = await offload_tool_results(self.transcript, working_state, self._cwd,
                                               result_directory=self.paths.blobs / 'tool-results')
            if result.transcript != self._transcript or working_state != self.context_state:
                self._transcript = result.transcript
                self._state.context_management = working_state
                self._mark_dirty()
            self.flush()
            return result.offloaded_count

    async def compact_context(
        self,
        *,
        provider: ChatProvider,
        visible_tools: list[ToolDefinition],
        deferred_tool_groups: list[DeferredToolGroup] | None = None,
        persist: bool = False,
        cancellation_token: "CancellationToken | None" = None,
    ) -> ContextCompactionResult:
        async with self._context_lock:
            previous_transcript = copy.deepcopy(self._transcript)
            previous_context = copy.deepcopy(self.context_state)
            previous_dirty = self._dirty
            before_request = self.build_request(
                visible_tools,
                allow_tool_calls=True,
                deferred_tool_groups=deferred_tool_groups,
            )
            before_tokens = self.estimate_request_tokens(before_request)
            compacted = await compact_transcript(
                provider=provider,
                model=self._provider_config.model,
                transcript=self.transcript,
                visible_tools=visible_tools,
                state=self.context_state,
                context_window=self.context_window,
                cancellation_token=cancellation_token,
            )
            self._transcript = compacted.transcript
            self.context_state.usage_anchor = None
            after_request = self.build_request(
                visible_tools,
                allow_tool_calls=True,
                deferred_tool_groups=deferred_tool_groups,
            )
            after_tokens = self.estimate_request_tokens(after_request)
            self._mark_dirty()
            try:
                self.flush(context_event='context.compacted')
            except Exception:
                self._transcript = previous_transcript
                self._state.context_management = previous_context
                self._dirty = previous_dirty
                raise
            return ContextCompactionResult(
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                dropped_groups=compacted.dropped_groups,
            )

    def total_usage(self) -> MessageUsage:
        total = MessageUsage()
        for message in self._state.messages:
            total.input_tokens += message.usage.input_tokens
            total.cached_input_tokens += message.usage.cached_input_tokens
            total.output_tokens += message.usage.output_tokens
        return total

    def _snapshot(self):
        return SessionCodec.encode(self._state, self._transcript,
                                   self._permission_storage.rules_for_scope('session'),
                                   self._selected_model_ref)

    def flush(self, *, force=True, context_event='context.replaced') -> None:
        if self._sessions.writer is None:
            return
        now = monotonic()
        if not force and now - self._last_flush < 0.25:
            return
        self._sessions.persist(self._snapshot(), context_event=context_event)
        self._dirty = False
        self._last_flush = now

    def record_event(self, kind, data=None, *, turn_id=None):
        self.flush()
        self._sessions.record(kind, data, turn_id=turn_id)

    def list_sessions(self):
        return self._sessions.repository.list_sessions()

    def new_session(self) -> None:
        self.close()
        self._sessions = SessionService(self._cwd)
        self._state = SessionState(work_phase=self._initial_phase, permission_policy=self._initial_policy)
        self._transcript = []
        self._active_dynamic_context = None
        self._permission_storage.replace_session_rules([], notify=False)
        self._dirty = False

    def rename_session(self, session_id: str, title: str) -> None:
        self.flush()
        self._sessions.rename(session_id, title)

    def archive_session(self, session_id: str) -> None:
        if session_id == self.session_id:
            raise SessionRepositoryError('请先新建或切换对话，再归档当前会话。')
        self._sessions.repository.archive(session_id)

    def remove_session(self, session_id: str) -> None:
        if session_id == self.session_id:
            raise SessionRepositoryError('不能删除当前正在使用的会话。')
        self._sessions.repository.remove(session_id)

    def read_session_model_ref(self, session_id: str) -> str | None:
        data = SessionCodec.project(self._sessions.repository.read(session_id))
        return SessionCodec.decode(data, session_id)[3]

    def resume_session(self, session_id: str, *, resolved_model=None) -> int:
        if session_id == self.session_id and self._sessions.writer is not None:
            return len(self._permission_storage.rules_for_scope('session'))
        self.flush()
        prepared = self._sessions.prepare(session_id)
        try:
            state, transcript, rules, model_ref = prepared[2]
            transcript = self._recover_interrupted_history(state, transcript)
            for item in state.pending_inputs:
                item.state = 'paused'
            if resolved_model is not None or model_ref != self._selected_model_ref:
                self._reset_model_context(state.context_management)
        except Exception:
            prepared[0].close()
            raise
        next_model_ref = resolved_model[1] if resolved_model is not None else model_ref
        candidate = SessionService(self._cwd)
        try:
            candidate.activate(prepared)
            candidate.persist(SessionCodec.encode(state, transcript, rules, next_model_ref))
        except Exception:
            candidate.close()
            raise
        self._sessions.close()
        self._sessions = candidate
        self._state, self._transcript = state, transcript
        self._permission_storage.replace_session_rules(rules, notify=False)
        self._active_dynamic_context = None
        self._selected_model_ref = next_model_ref
        if resolved_model is not None:
            self._provider_config = resolved_model[0]
        self._dirty = False
        return len(rules)

    def close(self) -> None:
        try:
            self.flush()
            self._sessions.checkpoint()
        finally:
            self._sessions.close()

    def _permissions_changed(self) -> None:
        self._mark_dirty()
        self.flush()

    @staticmethod
    def _recover_interrupted_history(
        state: SessionState, transcript: list[ConversationMessage]
    ) -> list[ConversationMessage]:
        """恢复自动保存的活动任务；补齐未知结果，绝不重放工具操作。"""
        interrupted_ids: set[str] = set()
        for message in state.messages:
            if message.role == "assistant" and message.status == "streaming":
                interrupted_ids.add(message.id)
                message.status = "cancelled"
                message.trace.collapsed = True
                if not message.content.strip():
                    message.content = "上次任务已中断。"
                for entry in list(message.trace.entries):
                    if entry.kind in {"text", "thinking"} and entry.metadata.get("state") == "streaming":
                        entry.metadata["state"] = "cancelled"
                    elif entry.kind == "tool_call" and entry.metadata.get("state") in {"queued", "running", "awaiting_permission"}:
                        entry.metadata["state"] = "cancelled"
                        message.trace.entries.append(TraceEntry(
                            kind="tool_result", call_id=entry.call_id, tool_name=entry.tool_name,
                            text="工具结果未完成", ok=False,
                            metadata={"group_id": entry.metadata.get("group_id"), "state": "cancelled",
                                      "started": entry.metadata.get("started", False),
                                      "error_code": "tool_result_interrupted",
                                      "content": "上次任务已中断，未获得完整结果；请先检查操作的实际状态。"},
                        ))
                message.trace.entries.append(TraceEntry(
                    kind="notice", text="会话恢复前的任务已中断；请先检查工具操作的实际状态。",
                ))
        if state.plan_snapshot is not None and state.plan_snapshot.source_message_id in interrupted_ids:
            state.plan_snapshot.ready = False

        recovered: list[ConversationMessage] = []
        cursor = 0
        while cursor < len(transcript):
            message = transcript[cursor]
            recovered.append(message)
            cursor += 1
            calls = [block for block in message.blocks if block.kind == "tool_use"]
            if message.role != "assistant" or not calls:
                continue
            # 调用标识可在后续批次复用，只在紧邻本批调用的结果中查找。
            result_message: ConversationMessage | None = None
            recorded_ids: set[str] = set()
            while cursor < len(transcript) and transcript[cursor].role == "tool":
                result_message = transcript[cursor]
                recovered.append(result_message)
                recorded_ids.update(block.call_id for block in result_message.blocks if block.kind == "tool_result")
                cursor += 1
            missing = [call for call in calls if call.call_id not in recorded_ids]
            if not missing:
                continue
            if result_message is None:
                result_message = ConversationMessage(role="tool", blocks=[])
                recovered.append(result_message)
            result_message.blocks.extend(ContentBlock.tool_result_block(
                call_id=call.call_id, is_error=True,
                text="上次任务在保存后中断，未获得此工具调用的完整结果。操作可能已部分执行，请先检查当前状态，勿直接重复执行。",
            ) for call in missing)
        return recovered

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _request_thinking(self) -> ThinkingConfig | None:
        if self._provider_config.protocol != "claude":
            return None
        return self._provider_config.thinking

    @staticmethod
    def _tool_result_content(result: ToolExecutionResult) -> str:
        if result.ok:
            return result.content.strip() or result.summary
        return result.error_message or result.content or result.summary

    @staticmethod
    def _expand_trace_on_first_entry(message: SessionMessage) -> None:
        if message.role == "assistant" and message.status == "streaming" and not message.trace.entries:
            message.trace.collapsed = False

    def _prompt_context(
        self, mode: RuntimeMode | None = None, *, work_phase: WorkPhase | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> "PromptContext":
        return build_prompt_context(
            cwd=self._cwd,
            current_date=self._current_date,
            runtime_mode=mode,
            work_phase=work_phase if work_phase is not None else self.work_phase,
            permission_policy=permission_policy if permission_policy is not None else self.permission_policy,
            plan_snapshot=self.plan_snapshot,
            plan_file_path=self.plan_file_path,
            session_id=self.session_id,
            session_workspace=self.paths.workspace if self.paths else None,
            previous_runtime_mode=self._state.previous_runtime_mode,
            plan_mode_turn_count=self._state.plan_mode_turn_count,
            pending_plan_entry_kind=self._state.pending_plan_entry_kind,
            pending_plan_exit_notice=self._state.pending_plan_exit_notice,
        )

    def _request_transcript(self) -> list[ConversationMessage]:
        messages: list[ConversationMessage] = []
        for message in self.transcript:
            blocks = list(message.blocks)
            if (
                message.role == "user"
                and len(blocks) > 1
                and blocks[0].kind == "text"
                and blocks[0].text.startswith("<system-reminder>\n")
            ):
                blocks = blocks[1:]
            messages.append(ConversationMessage(role=message.role, blocks=blocks))
        if self._active_dynamic_context:
            for message in reversed(messages):
                if message.role == "user":
                    message.blocks.insert(0, ContentBlock.text_block(self._active_dynamic_context))
                    break
        return messages

    def _advance_dynamic_prompt_state_after_user_turn(self) -> None:
        if self.work_phase == "plan":
            self._state.plan_mode_turn_count += 1
            self._state.pending_plan_entry_kind = None
            return

        self._state.pending_plan_exit_notice = False

    @staticmethod
    def _filter_tools_for_mode(tools: list[ToolDefinition], mode: RuntimeMode) -> list[ToolDefinition]:
        return [tool for tool in tools if mode in tool.allowed_modes]

    @staticmethod
    def _new_message_id() -> str:
        return uuid4().hex

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
