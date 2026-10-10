from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from time import monotonic
from uuid import uuid4

from lancher_code.context.models import (
    CompactionActivity,
    CompactionStatus,
    CompactionTrigger,
    ContextCompactionResult,
    ContextManagementState,
)
from lancher_code.context.offload import offload_tool_results
from lancher_code.context.prompt_models import PromptContext
from lancher_code.context.prompts import build_dynamic_context_prompt, build_prompt_context, build_user_message
from lancher_code.context.recovery import record_file_snapshot
from lancher_code.context.request import build_request
from lancher_code.context.tokens import estimate_request, estimate_request_tokens, update_usage_anchor
from lancher_code.contracts.control import CancellationToken, PermissionPolicy, WorkPhase, validate_runtime_axes
from lancher_code.contracts.messages import ChatRequest, ContentBlock, ConversationMessage
from lancher_code.contracts.tools import DeferredToolGroup, ToolCall, ToolDefinition, ToolExecutionResult
from lancher_code.logging_system import get_logger
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.providers.base import ChatProvider
from lancher_code.providers.models import ProviderConfig
from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.compaction import prepare_compaction
from lancher_code.sessions.messages import MessageEditor
from lancher_code.sessions.models import PendingInput, PlanSnapshot, SessionMessage, SessionState, TraceEntry
from lancher_code.sessions.recovery import recover_interrupted_history
from lancher_code.sessions.service import SessionService
from lancher_code.sessions.storage import SessionRepositoryError
from lancher_code.usage.ledger import RequestUsageRecord, RunUsageTracker, summarize_records
from lancher_code.usage.models import MessageUsage
from lancher_code.usage.tracking import tracked_stream


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
        initial_work_phase: WorkPhase | None = None,
        initial_permission_policy: PermissionPolicy | None = None,
        permission_storage: PermissionStorage | None = None,
        selected_model_ref: str | None = None,
        usage_tracker: RunUsageTracker | None = None,
    ) -> None:
        self._provider_config = provider_config
        self._selected_model_ref = selected_model_ref
        self._state = state or SessionState()
        self._cwd = (cwd or Path.cwd()).resolve()
        self._current_date = current_date or datetime.now().astimezone().date()
        self._transcript: list[ConversationMessage] = []
        self._sessions = SessionService(self._cwd)
        self._usage_tracker = usage_tracker or RunUsageTracker()
        self._visited_session_ids: list[str] = []
        self._execution_runtime = None
        self._last_flush = monotonic()
        self._dirty = False
        self._context_lock = asyncio.Lock()
        self._active_dynamic_context: str | None = None
        self._permission_storage = permission_storage or PermissionStorage()
        self._permission_storage.subscribe_session_rules_changed(self._permissions_changed)
        if initial_work_phase is not None or initial_permission_policy is not None:
            phase = initial_work_phase or self.work_phase
            policy = initial_permission_policy or self.permission_policy
            validate_runtime_axes(phase, policy)
            self.set_permission_policy(policy)
            self.set_work_phase(phase)
        self._initial_phase = self.work_phase
        self._initial_policy = self.permission_policy

    @property
    def project_root(self) -> Path:
        return self._cwd

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def transcript(self) -> list[ConversationMessage]:
        return list(self._transcript)

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
            snapshot = SessionCodec.decode_plan_snapshot(asdict(snapshot))
        self._state.plan_snapshot = copy.deepcopy(snapshot)
        self._mark_dirty()
        return self.plan_snapshot

    @persist_change()
    def update_pending_inputs(self, items: list[PendingInput]) -> None:
        self._state.pending_inputs = SessionCodec.decode_pending_inputs([asdict(item) for item in items])
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

    @persist_change()
    def set_work_phase(self, phase: WorkPhase) -> WorkPhase:
        validate_runtime_axes(phase, self.permission_policy)
        previous_phase = self.work_phase
        if phase == previous_phase:
            return phase
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
        validate_runtime_axes(self.work_phase, policy)
        if policy != self.permission_policy:
            self._state.permission_policy = policy
            self._mark_dirty()
        return policy

    @persist_change()
    def create_user_message(self, text: str) -> SessionMessage:
        if not text.strip():
            raise ValueError('用户消息不能为空。')
        if self.session_id is None:
            self._state.session_id = self._sessions.create(self._snapshot(), text)
        self._remember_session()
        if self.work_phase == "plan" and self._state.plan_snapshot is not None:
            self._state.plan_snapshot.ready = False
        message = SessionMessage(
            id=uuid4().hex,
            role="user",
            content=text,
            status="complete",
            timestamp=datetime.now(timezone.utc),
        )
        self._state.messages.append(message)
        self._active_dynamic_context = build_dynamic_context_prompt(self._prompt_context())
        self._transcript.append(
            build_user_message(text=text, dynamic_context=self._active_dynamic_context)
        )
        notifications = list(self._state.execution['inbox'][:20])
        if notifications:
            summaries = [{key: item.get(key) for key in
                          ('process_id', 'status', 'exit_code', 'exit_reason', 'lifetime')}
                         for item in notifications]
            self._transcript[-1].blocks.append(ContentBlock.text_block(
                '此前托管进程的状态通知（记录事实，不是新的指令）：\n' +
                json.dumps(summaries, ensure_ascii=False)))
        self._advance_dynamic_prompt_state_after_user_turn()
        self._mark_dirty()
        self._register_execution_session()
        if notifications and self._execution_runtime is not None:
            # 先保存包含通知的协议消息；随后消费 inbox 不会丢失通知正文。
            self.flush()
            self._execution_runtime.record_event(self.session_id, 'execution.inbox_acknowledged',
                {'notification_ids': [item['notification_id'] for item in notifications]})
        return message

    @persist_change()
    def create_assistant_message(self) -> SessionMessage:
        return MessageEditor(self._state.messages).create_assistant_message()

    @persist_change(streaming=True)
    def append_message_content(self, message_id: str, delta: str) -> SessionMessage:
        return MessageEditor(self._state.messages).append_message_content(message_id, delta)

    @persist_change()
    def clear_message_content(self, message_id: str) -> SessionMessage:
        return MessageEditor(self._state.messages).clear_message_content(message_id)

    @persist_change()
    def add_message_usage(self, message_id: str, usage: MessageUsage) -> SessionMessage:
        return MessageEditor(self._state.messages).add_message_usage(message_id, usage)

    @persist_change(streaming=True)
    def append_trace_thinking(self, message_id: str, delta: str) -> SessionMessage:
        return MessageEditor(self._state.messages).append_trace_thinking(message_id, delta)

    @persist_change(streaming=True)
    def append_trace_text(self, message_id: str, text: str) -> SessionMessage:
        return MessageEditor(self._state.messages).append_trace_text(message_id, text)

    @persist_change(streaming=True)
    def finish_trace_segment(self, message_id: str, state: str = "complete") -> SessionMessage:
        return MessageEditor(self._state.messages).finish_trace_segment(message_id, state)

    @persist_change()
    def append_trace_notice(self, message_id: str, text: str) -> SessionMessage:
        return MessageEditor(self._state.messages).append_trace_notice(message_id, text)

    @persist_change()
    def append_trace_tool_calls(self, message_id: str, tool_calls: list[ToolCall]) -> SessionMessage:
        return MessageEditor(self._state.messages).append_trace_tool_calls(message_id, tool_calls)

    @persist_change()
    def set_trace_tool_state(self, message_id: str, call_id: str, state: str,
                             *, waiting: dict | None = None, invocation_id: str | None = None) -> SessionMessage:
        return MessageEditor(self._state.messages).set_trace_tool_state(message_id, call_id, state, waiting=waiting, invocation_id=invocation_id)

    @persist_change()
    def append_trace_tool_results(self, message_id: str, results: list[ToolExecutionResult]) -> SessionMessage:
        return MessageEditor(self._state.messages).append_trace_tool_results(message_id, results)

    @persist_change()
    def append_assistant_response(self, blocks: list[ContentBlock]) -> None:
        """保存一次完整助手交换，包含工具调用前的思考与文字。"""
        if not blocks:
            return
        # 提供方/runner 随后仍会更新流式缓冲和工具参数，持久化快照不能
        # 与这些可变对象共用引用，也不能用界面上的累计文本重建协议。
        response = ConversationMessage(
            role="assistant", blocks=copy.deepcopy(blocks), response_protocol=self._provider_config.protocol,
            response_model=self._provider_config.model,
        )
        # 完整快照也可能来自损坏的流；先验证再修改内存/写事件，不能
        # 把空思考等无效协议落成下次无法恢复的会话记录。
        SessionCodec.decode_transcript(asdict(response))
        self._transcript.append(response)

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
    def complete_message(
        self, message_id: str, usage: MessageUsage | None = None, *, record_transcript: bool = True
    ) -> SessionMessage:
        message = self.finish_trace_segment(message_id)
        message.status = "complete"
        message.trace.collapsed = True
        if usage is not None:
            message.usage = copy.deepcopy(usage)
        if record_transcript and message.content.strip():
            self.append_assistant_response([ContentBlock.text_block(message.content)])
        self._active_dynamic_context = None
        return message

    @persist_change()
    def fail_message(self, message_id: str, error_text: str) -> SessionMessage:
        message = self.finish_trace_segment(message_id, "error")
        if not MessageEditor.has_last_notice(message, error_text):
            self.append_trace_notice(message_id, error_text)
        message.status = "error"
        message.content = error_text
        message.trace.collapsed = True
        self._active_dynamic_context = None
        return message

    @persist_change()
    def cancel_message(self, message_id: str, notice_text: str = "本轮已取消。") -> SessionMessage:
        message = self.finish_trace_segment(message_id, "cancelled")
        for entry in message.trace.entries:
            if entry.kind == "tool_call":
                if entry.metadata.get("state") in {"queued", "running", "awaiting_permission", "waiting_resources"}:
                    entry.metadata["state"] = "cancelled"
                entry.metadata.pop("waiting", None)
        if not MessageEditor.has_last_notice(message, notice_text):
            self.append_trace_notice(message_id, notice_text)
        message.status = "cancelled"
        if not message.content.strip():
            message.content = notice_text
        message.trace.collapsed = True
        self._active_dynamic_context = None
        return message

    def get_message(self, message_id: str) -> SessionMessage:
        return MessageEditor(self._state.messages).get_message(message_id)

    def build_request(
        self, tools: list[ToolDefinition], *, allow_tool_calls: bool,
        work_phase: WorkPhase | None = None, permission_policy: PermissionPolicy | None = None,
        deferred_tool_groups: list[DeferredToolGroup] | None = None,
    ) -> ChatRequest:
        phase = self.work_phase if work_phase is None else work_phase
        policy = self.permission_policy if permission_policy is None else permission_policy
        validate_runtime_axes(phase, policy)
        return build_request(
            config=self._provider_config, context=self._prompt_context(work_phase=phase, permission_policy=policy),
            transcript=self._transcript, state=self.context_state, dynamic_context=self._active_dynamic_context,
            tools=tools, allow_tool_calls=allow_tool_calls, deferred_tool_groups=deferred_tool_groups,
        )

    def estimate_request_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request, self.context_state)

    def context_estimate(self, request: ChatRequest):
        # 界面查看不同工具集合时，不能清空下一次真实请求仍可用的锚点。
        return estimate_request(request, copy.deepcopy(self.context_state))

    def bind_usage_request(self, request: ChatRequest, *, turn_id=None, message_id=None, purpose=None) -> ChatRequest:
        """回调捕获请求所属会话，不能借当前界面状态把并发请求记到别处。"""
        request.session_id = self.session_id
        request.run_id = self._usage_tracker.run_id
        request.request_id = request.request_id or uuid4().hex
        request.turn_id = turn_id
        request.message_id = message_id
        if purpose is not None:
            request.purpose = purpose
        state, service = self._state, self._sessions

        def record_usage(data):
            record = RequestUsageRecord.from_dict(data)
            if record.session_id != state.session_id:
                raise ValueError("请求用量的 Session 归属不一致。")
            encoded = record.to_dict()
            if state.request_usage.get(record.request_id) == encoded:
                return
            # 先验证运行归属与请求身份，拒绝坏回调污染会话日志。即使随后
            # 磁盘保存失败，本次启动仍保留已经观察到的实际用量。
            self._usage_tracker.accept_record(record)
            service.record_usage(encoded, turn_id=record.turn_id)
            state.request_usage[record.request_id] = encoded
            if record.message_id is not None:
                related = [RequestUsageRecord.from_dict(item) for item in state.request_usage.values()
                           if item.get("message_id") == record.message_id]
                target = next((item for item in state.messages if item.id == record.message_id), None)
                if target is not None:
                    target.usage = summarize_records(related).usage
            if state is self._state:
                self._mark_dirty()
                self.flush(force=record.status != "running")

        request.usage_callback = record_usage
        return request

    def stream_request(self, provider: ChatProvider, request: ChatRequest):
        return tracked_stream(provider, request, protocol=self._provider_config.protocol,
                              run_id=self._usage_tracker.run_id)

    @persist_change()
    def update_context_usage(self, request: ChatRequest, usage: MessageUsage) -> None:
        update_usage_anchor(self.context_state, request, usage)
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
            offloaded_count = await offload_tool_results(self.transcript, working_state, self._cwd,
                                               result_directory=self.paths.blobs / 'tool-results',
                                               context_window=self.context_window)
            if working_state != self.context_state:
                self._state.context_management = working_state
                self._mark_dirty()
            self.flush()
            return offloaded_count

    async def compact_context(
        self,
        *,
        provider: ChatProvider,
        visible_tools: list[ToolDefinition],
        deferred_tool_groups: list[DeferredToolGroup] | None = None,
        cancellation_token: "CancellationToken | None" = None,
        turn_id: str | None = None,
        activity_id: str | None = None,
    ) -> ContextCompactionResult:
        # Runner 负责已有活动的事件发布；直接调用 Controller 的入口也
        # 必须完整收尾，不能在摘要报错或取消后遗留永久 running。
        created_activity = activity_id is None
        if activity_id is None:
            activity_id = self.begin_compaction('manual', turn_id=turn_id).id
        try:
            return await self._compact_context_candidate(
                provider=provider, visible_tools=visible_tools,
                deferred_tool_groups=deferred_tool_groups,
                cancellation_token=cancellation_token, turn_id=turn_id, activity_id=activity_id,
            )
        except asyncio.CancelledError:
            if created_activity:
                self.finish_compaction(activity_id, status='cancelled')
            raise
        except Exception as exc:
            if created_activity:
                self.finish_compaction(activity_id, status='failed', error_text=str(exc))
            raise

    async def _compact_context_candidate(
        self, *, provider: ChatProvider, visible_tools: list[ToolDefinition],
        deferred_tool_groups: list[DeferredToolGroup] | None,
        cancellation_token: "CancellationToken | None", turn_id: str | None,
        activity_id: str,
    ) -> ContextCompactionResult:
        async with self._context_lock:
            activity = self._state.compaction_activities[activity_id]
            if activity.status != 'running':
                raise ValueError('该压缩活动已经结束，不能重新执行。')
            previous_transcript = copy.deepcopy(self._transcript)
            previous_context = copy.deepcopy(self.context_state)
            previous_dirty = self._dirty
            before_request = self.build_request(
                visible_tools,
                allow_tool_calls=True,
                deferred_tool_groups=deferred_tool_groups,
            )
            before_estimate = self.context_estimate(before_request)
            before_tokens = before_estimate.tokens
            activity.before_tokens = before_tokens
            activity.before_source = before_estimate.source
            self._mark_dirty()
            self.flush()
            candidate = await prepare_compaction(
                config=self._provider_config, prompt_context=self._prompt_context(),
                transcript=previous_transcript, context=previous_context,
                dynamic_context=self._active_dynamic_context, before_estimate=before_estimate,
                visible_tools=visible_tools, deferred_tool_groups=deferred_tool_groups,
                cancellation_token=cancellation_token,
                stream_request=lambda request: self.stream_request(provider, request),
                request_factory=lambda request: self.bind_usage_request(request, turn_id=turn_id, purpose="compaction"),
            )
            result = candidate.result
            self._transcript = candidate.transcript
            self._state.context_management = candidate.context
            try:
                previous_activity = copy.deepcopy(activity)
                self._apply_compaction_result(activity, result)
                activity.status = 'completed'
                activity.finished_at = datetime.now(timezone.utc)
                self._mark_dirty()
                self.flush(context_event='context.compacted', context_activity_id=activity_id)
            except Exception:
                durable = self._sessions.compaction_committed(activity_id)
                if durable:
                    # 完成提交之后，显示/运行时挂接的错误不能把已持久化
                    # 的上下文撤回；runner 再次终结时会拿到 completed。
                    raise
                self._transcript = previous_transcript
                self._state.context_management = previous_context
                if 'previous_activity' in locals():
                    self._state.compaction_activities[activity_id] = previous_activity
                self._dirty = previous_dirty
                raise
            return result

    def begin_compaction(
        self, trigger: CompactionTrigger, *, message_id: str | None = None, turn_id: str | None = None,
    ) -> CompactionActivity:
        """创建一次操作活动，空对话只在内存保留，不提前创建 Session。"""
        if trigger not in {'manual', 'automatic', 'emergency'}:
            raise ValueError('压缩触发方式无效。')
        if message_id is not None:
            message = self.get_message(message_id)
            if message.role != 'assistant':
                raise ValueError('压缩活动只能关联助手消息。')
        elif trigger != 'manual':
            raise ValueError('自动压缩活动必须关联当前助手消息。')
        activity = CompactionActivity(
            id=uuid4().hex, trigger=trigger, status='running', started_at=datetime.now(timezone.utc),
            message_id=message_id, turn_id=turn_id,
            after_message_id=self._state.messages[-1].id if message_id is None and self._state.messages else None,
        )
        self._state.compaction_activities[activity.id] = activity
        if message_id is not None:
            message = self.get_message(message_id)
            # 封口流式正文后插入活动，后续正文会形成新的输出段。
            if message.trace.entries:
                last = message.trace.entries[-1]
                if last.kind in {'text', 'thinking'} and last.metadata.get('state') == 'streaming':
                    last.metadata['state'] = 'complete'
            MessageEditor.expand_trace_on_first_entry(message)
            message.trace.entries.append(TraceEntry(kind='compaction', metadata={'activity_id': activity.id}))
        self._mark_dirty()
        try:
            self.flush()
        except Exception as exc:
            # 启动保存失败时，还没有 worker 能替它收尾。保留同一活动
            # 的内存失败事实供界面显示，不在故障分支再次尝试写盘。
            activity.status = 'failed'
            activity.finished_at = datetime.now(timezone.utc)
            activity.error_text = str(exc)
            self._mark_dirty()
            raise
        return copy.deepcopy(activity)

    def get_compaction(self, activity_id: str) -> CompactionActivity:
        """事件携带独立快照，已发出的 running 不能随共享引用变成完成。"""
        return copy.deepcopy(self._state.compaction_activities[activity_id])

    def finish_compaction(
        self, activity_id: str, *, status: CompactionStatus,
        result: ContextCompactionResult | None = None, error_text: str | None = None,
        continued: bool | None = None,
    ) -> CompactionActivity:
        if status not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            raise ValueError('压缩活动必须以终态结束。')
        activity = self._state.compaction_activities[activity_id]
        if activity.status != 'running':
            # 停止和成功提交可能先后抵达；第一次终态就是持久化事实。
            if continued and activity.status == 'failed' and activity.trigger != 'manual' and not activity.continued:
                activity.continued = True
                self._mark_dirty()
                self.flush()
            return copy.deepcopy(activity)
        if continued and (status != 'failed' or activity.trigger == 'manual'):
            raise ValueError('只有自动压缩失败可以标记继续本轮。')
        if status == 'completed':
            if result is None:
                raise ValueError('完成压缩必须提供前后估算。')
            self._apply_compaction_result(activity, result)
        activity.status = status
        activity.finished_at = None if status == 'interrupted' else datetime.now(timezone.utc)
        activity.error_text = error_text
        activity.continued = bool(continued)
        self._mark_dirty()
        self.flush()
        return copy.deepcopy(activity)

    @staticmethod
    def _apply_compaction_result(activity: CompactionActivity, result: ContextCompactionResult) -> None:
        for name in ('before_tokens', 'after_tokens', 'dropped_groups'):
            value = getattr(result, name)
            if type(value) is not int or value < 0:
                raise ValueError('压缩结果必须包含非负整数计数。')
        if any(getattr(result, name) not in {'estimated', 'usage_calibrated'}
               for name in ('before_source', 'after_source')):
            raise ValueError('压缩结果的估算来源无效。')
        for name in ('before_tokens', 'after_tokens', 'before_source', 'after_source', 'dropped_groups'):
            setattr(activity, name, getattr(result, name))

    def usage_summary(self, message_id: str | None = None):
        records = [RequestUsageRecord.from_dict(item) for item in self._state.request_usage.values()
                   if message_id is None or item.get("message_id") == message_id]
        return summarize_records(records)

    def total_usage(self):
        return self.usage_summary()

    def _snapshot(self):
        return SessionCodec.encode(self._state, self._transcript,
                                   self._permission_storage.rules_for_scope('session'),
                                   self._selected_model_ref)

    def flush(self, *, force=True, context_event='context.replaced', context_activity_id=None) -> None:
        if self._sessions.writer is None:
            return
        now = monotonic()
        if not force and now - self._last_flush < 0.25:
            return
        self._sessions.persist(self._snapshot(), context_event=context_event, context_activity_id=context_activity_id)
        self._dirty = False
        self._last_flush = now
        self._register_execution_session()

    def bind_execution_runtime(self, runtime) -> None:
        self._execution_runtime = runtime
        self._register_execution_session()

    def _register_execution_session(self) -> None:
        if self._execution_runtime is None or self.session_id is None or self._sessions.writer is None:
            return
        from lancher_code.execution.runtime import SessionRuntime
        self._execution_runtime.register_session(SessionRuntime(
            self._sessions, self._state, self._transcript,
            self._permission_storage.rules_for_scope('session'),
            self._selected_model_ref, self._provider_config))

    def _prepare_detach_view(self) -> None:
        # 切换失败必须留下可继续持久化的当前会话；checkpoint 成功才关闭写入者。
        self.flush()
        self._sessions.checkpoint()

    def _detach_view(self, *, prepared=False) -> None:
        if not prepared:
            self._prepare_detach_view()
        if self._execution_runtime is not None and self.session_id is not None:
            self._register_execution_session()
            self._execution_runtime.detach_view(self.session_id)
            if self._execution_runtime.processes.active_session(self.session_id):
                return
            self._execution_runtime.sessions.release(self.session_id, checkpoint=False)
        else:
            self._sessions.close()

    def record_event(self, kind, data=None, *, turn_id=None):
        self.flush()
        self._sessions.record(kind, data, turn_id=turn_id)

    def list_sessions(self):
        return self._sessions.repository.list_sessions()

    @property
    def visited_session_ids(self) -> tuple[str, ...]:
        """本次运行实际使用过的会话，按最近使用排列；不包含磁盘上的其他对话。"""
        return tuple(self._visited_session_ids)

    def _remember_session(self) -> None:
        session_id = self.session_id
        if session_id is not None:
            if session_id in self._visited_session_ids:
                self._visited_session_ids.remove(session_id)
            self._visited_session_ids.append(session_id)

    def new_session(self) -> None:
        self._detach_view()
        self._sessions = SessionService(self._cwd)
        self._state = SessionState(work_phase=self._initial_phase, permission_policy=self._initial_policy)
        self._transcript = []
        self._active_dynamic_context = None
        self._permission_storage.replace_session_rules([], notify=False)
        self._dirty = False

    def rename_session(self, session_id: str, title: str) -> None:
        self.flush()
        binding = self._execution_runtime.sessions.get(session_id) if self._execution_runtime else None
        (binding.service if binding else self._sessions).rename(session_id, title)

    def archive_session(self, session_id: str) -> None:
        if session_id == self.session_id:
            raise SessionRepositoryError('请先新建或切换对话，再归档当前会话。')
        self._release_inactive_execution(session_id)
        self._sessions.repository.archive(session_id)

    def remove_session(self, session_id: str) -> None:
        if session_id == self.session_id:
            raise SessionRepositoryError('不能删除当前正在使用的会话。')
        self._release_inactive_execution(session_id)
        self._sessions.repository.remove(session_id)

    def _release_inactive_execution(self, session_id):
        if self._execution_runtime is not None:
            if self._execution_runtime.processes.active_session(session_id):
                raise SessionRepositoryError('该会话还有运行中的进程，请先停止会话任务。')
            self._execution_runtime.sessions.release(session_id)

    def read_session_model_ref(self, session_id: str) -> str | None:
        data = SessionCodec.project(self._sessions.repository.read(session_id))
        return SessionCodec.decode(data, session_id)[3]

    def resume_session(self, session_id: str, *, resolved_model=None) -> int:
        if session_id == self.session_id and self._sessions.writer is not None:
            self._remember_session()
            return len(self._permission_storage.rules_for_scope('session'))
        self.flush()
        retained = self._execution_runtime.sessions.get(session_id) if self._execution_runtime else None
        if retained is not None:
            self._prepare_detach_view()
            next_ref = resolved_model[1] if resolved_model is not None else retained.model_ref
            next_provider = resolved_model[0] if resolved_model is not None else retained.provider_config
            next_state = retained.state
            if next_ref != retained.model_ref or next_provider != retained.provider_config:
                next_state = copy.deepcopy(retained.state)
                self._reset_model_context(next_state.context_management)
            retained.service.persist(SessionCodec.encode(next_state, retained.transcript, retained.rules, next_ref))
            self._detach_view(prepared=True)
            self._sessions, self._state, self._transcript = retained.service, next_state, retained.transcript
            self._permission_storage.replace_session_rules(retained.rules, notify=False)
            self._selected_model_ref, self._provider_config = next_ref, next_provider
            self._active_dynamic_context = None
            self._dirty = False
            self._register_execution_session()
            self._remember_session()
            return len(retained.rules)
        prepared = self._sessions.prepare(session_id)
        try:
            state, transcript, rules, model_ref = prepared[2]
            transcript = recover_interrupted_history(state, transcript)
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
            self._prepare_detach_view()
            candidate.activate(prepared)
            candidate.persist(SessionCodec.encode(state, transcript, rules, next_model_ref))
            if self._execution_runtime is not None:
                from lancher_code.execution.runtime import SessionRuntime
                self._execution_runtime.register_session(SessionRuntime(
                    candidate, state, transcript, rules, next_model_ref,
                    resolved_model[0] if resolved_model is not None else self._provider_config), viewed=False)
                self._execution_runtime.recover_session(session_id)
            self._detach_view(prepared=True)
        except Exception:
            if self._execution_runtime is not None and self._execution_runtime.sessions.get(session_id) is not None:
                self._execution_runtime.sessions.release(session_id, checkpoint=False)
            if candidate.writer is not None:
                candidate.close()
            else:
                prepared[0].close()
            raise
        self._sessions = candidate
        self._state, self._transcript = state, transcript
        self._permission_storage.replace_session_rules(rules, notify=False)
        self._active_dynamic_context = None
        self._selected_model_ref = next_model_ref
        if resolved_model is not None:
            self._provider_config = resolved_model[0]
        self._dirty = False
        self._register_execution_session()
        self._remember_session()
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

    def _mark_dirty(self) -> None:
        self._dirty = True

    @staticmethod
    def _tool_result_content(result: ToolExecutionResult) -> str:
        if (not result.is_error):
            return result.content.strip() or result.summary
        return result.error_message or result.content or result.summary

    def _prompt_context(
        self, *, work_phase: WorkPhase | None = None,
        permission_policy: PermissionPolicy | None = None,
    ) -> "PromptContext":
        return build_prompt_context(
            cwd=self._cwd,
            current_date=self._current_date,
            work_phase=work_phase if work_phase is not None else self.work_phase,
            permission_policy=permission_policy if permission_policy is not None else self.permission_policy,
            plan_snapshot=self.plan_snapshot,
            plan_file_path=self.plan_file_path,
            session_id=self.session_id,
            session_workspace=self.paths.workspace if self.paths else None,
            plan_mode_turn_count=self._state.plan_mode_turn_count,
            pending_plan_entry_kind=self._state.pending_plan_entry_kind,
            pending_plan_exit_notice=self._state.pending_plan_exit_notice,
        )

    def _advance_dynamic_prompt_state_after_user_turn(self) -> None:
        if self.work_phase == "plan":
            self._state.plan_mode_turn_count += 1
            self._state.pending_plan_entry_kind = None
            return

        self._state.pending_plan_exit_notice = False
