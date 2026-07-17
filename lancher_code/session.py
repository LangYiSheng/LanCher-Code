from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timezone
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from lancher_code.models import (
    ChatRequest,
    ContentBlock,
    ContextCompactionResult,
    ContextFileSnapshot,
    ContextManagementState,
    ContextUsageAnchor,
    ConversationMessage,
    DeferredToolGroup,
    MessageUsage,
    PermissionRule,
    ProviderConfig,
    PromptContext,
    RuntimeMode,
    SessionMessage,
    SessionState,
    ThinkingConfig,
    ToolCall,
    ToolDefinition,
    ToolExecutionResult,
    ToolResultReplacement,
    TraceEntry,
    ThinkingTrace,
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


logger = get_logger("session")
from lancher_code.permission_engine import PermissionStorage
from lancher_code.prompting import (
    build_chat_request_payload,
    build_dynamic_context_prompt,
    build_prompt_context,
    build_user_message,
)
from lancher_code.session_store import (
    SESSION_FORMAT_VERSION,
    SUPPORTED_SESSION_FORMAT_VERSIONS,
    ProjectSessionStore,
    SessionStoreError,
    StoredSessionInfo,
    utc_now,
)


class SessionController:
    """管理当前进程内的会话状态与协议无关 transcript。"""

    def __init__(
        self,
        provider_config: ProviderConfig,
        state: SessionState | None = None,
        *,
        cwd: Path | None = None,
        current_date: date | None = None,
        plan_file_path: Path | None = None,
        initial_runtime_mode: RuntimeMode = "default",
        permission_storage: PermissionStorage | None = None,
    ) -> None:
        self._provider_config = provider_config
        self._state = state or SessionState()
        self._cwd = (cwd or Path.cwd()).resolve()
        self._current_date = current_date or datetime.now().astimezone().date()
        self._plan_file_path = self._resolve_plan_file_path(plan_file_path)
        self._transcript: list[ConversationMessage] = []
        self._session_store = ProjectSessionStore(self._cwd)
        self._active_session_name: str | None = None
        self._session_created_at: datetime | None = None
        self._dirty = False
        self._context_lock = asyncio.Lock()
        self._active_dynamic_context: str | None = None
        self._permission_storage = permission_storage or PermissionStorage()
        self._permission_storage.subscribe_session_rules_changed(self._mark_dirty)
        if initial_runtime_mode != self._state.runtime_mode:
            self.set_runtime_mode(initial_runtime_mode)

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
    def plan_file_path(self) -> Path:
        return self._plan_file_path

    @property
    def active_session_name(self) -> str | None:
        return self._active_session_name

    @property
    def has_unsaved_changes(self) -> bool:
        return self._dirty

    @property
    def context_state(self) -> ContextManagementState:
        return self._state.context_management

    @property
    def context_window(self) -> int:
        return self._provider_config.context_window

    def set_runtime_mode(self, mode: RuntimeMode) -> RuntimeMode:
        previous_mode = self._state.runtime_mode
        if mode == previous_mode:
            return mode

        self._state.previous_runtime_mode = previous_mode
        if mode == "plan" and previous_mode != "plan":
            self._state.plan_restore_mode = previous_mode
            self._state.pending_plan_entry_kind = "reentry" if self._plan_file_path.exists() else "initial"
            self._state.pending_plan_exit_notice = False
            self._state.plan_mode_turn_count = 0
        elif previous_mode == "plan" and mode != "plan":
            self._state.pending_plan_exit_notice = self._state.plan_mode_turn_count > 0
            self._state.pending_plan_entry_kind = None

        self._state.runtime_mode = mode
        self._mark_dirty()
        self.auto_save()
        return mode

    def restore_mode_after_plan(self) -> RuntimeMode:
        restore_mode = self._state.plan_restore_mode
        self.set_runtime_mode(restore_mode)
        return restore_mode

    def create_user_message(self, text: str) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="user",
            content=text,
            status="complete",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        self._active_dynamic_context = build_dynamic_context_prompt(self._prompt_context(self.runtime_mode))
        self._transcript.append(
            build_user_message(text=text, dynamic_context=self._active_dynamic_context)
        )
        self._advance_dynamic_prompt_state_after_user_turn()
        self._mark_dirty()
        return message

    def create_assistant_message(self) -> SessionMessage:
        message = SessionMessage(
            id=self._new_message_id(),
            role="assistant",
            content="",
            status="streaming",
            timestamp=self._now(),
        )
        self._state.messages.append(message)
        self._mark_dirty()
        return message

    def append_message_content(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content += delta
        return message

    def clear_message_content(self, message_id: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.content = ""
        return message

    def add_message_usage(self, message_id: str, usage: MessageUsage) -> SessionMessage:
        message = self.get_message(message_id)
        message.usage.input_tokens += usage.input_tokens
        message.usage.cached_input_tokens += usage.cached_input_tokens
        message.usage.output_tokens += usage.output_tokens
        return message

    def append_trace_thinking(self, message_id: str, delta: str) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        entries = message.trace.entries
        if entries and entries[-1].kind == "thinking":
            entries[-1].text += delta
        else:
            entries.append(TraceEntry(kind="thinking", text=delta))
        return message

    def append_trace_text(self, message_id: str, text: str) -> SessionMessage:
        message = self.get_message(message_id)
        if text:
            self._expand_trace_on_first_entry(message)
            message.trace.entries.append(TraceEntry(kind="text", text=text))
        return message

    def append_trace_notice(self, message_id: str, text: str) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        message.trace.entries.append(TraceEntry(kind="notice", text=text))
        return message

    def append_trace_tool_calls(self, message_id: str, tool_calls: list[ToolCall]) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        for call in tool_calls:
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_call",
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                )
            )
        return message

    def append_trace_tool_results(self, message_id: str, results: list[ToolExecutionResult]) -> SessionMessage:
        message = self.get_message(message_id)
        self._expand_trace_on_first_entry(message)
        for result in results:
            message.trace.entries.append(
                TraceEntry(
                    kind="tool_result",
                    call_id=result.call_id,
                    tool_name=result.tool_name,
                    text=result.summary if result.ok else (result.error_message or result.summary),
                    metadata=result.metadata,
                    ok=result.ok,
                )
            )
        return message

    def append_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        if not tool_calls:
            return

        blocks = [
            ContentBlock.tool_use_block(call_id=call.call_id, name=call.tool_name, input=call.arguments)
            for call in tool_calls
        ]
        self._transcript.append(ConversationMessage(role="assistant", blocks=blocks))

    def append_tool_results(self, results: list[ToolExecutionResult]) -> None:
        for result in results:
            self._transcript.append(
                ConversationMessage(
                    role="tool",
                    blocks=[
                        ContentBlock.tool_result_block(
                            call_id=result.call_id,
                            text=self._tool_result_content(result),
                            is_error=result.is_error,
                        )
                    ],
                )
            )

    def complete_message(self, message_id: str, usage: MessageUsage | None = None) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "complete"
        message.trace.collapsed = True
        message.usage = usage or MessageUsage()
        if message.content.strip():
            self._transcript.append(ConversationMessage.text_message("assistant", message.content))
        self._active_dynamic_context = None
        return message

    def fail_message(self, message_id: str, error_text: str) -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "error"
        message.content = error_text
        message.trace.collapsed = True
        self._active_dynamic_context = None
        return message

    def cancel_message(self, message_id: str, notice_text: str = "本轮已取消。") -> SessionMessage:
        message = self.get_message(message_id)
        message.status = "cancelled"
        if not message.content.strip():
            message.content = notice_text
        message.trace.collapsed = True
        self._active_dynamic_context = None
        return message

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
        deferred_tool_groups: list[DeferredToolGroup] | None = None,
    ) -> ChatRequest:
        active_mode = mode or self.runtime_mode
        thinking = self._request_thinking()
        if not allow_tool_calls:
            filtered_tools = []
        elif all(active_mode in tool.allowed_modes for tool in tools):
            filtered_tools = tools
        else:
            filtered_tools = self._filter_tools_for_mode(tools, active_mode)
        payload = build_chat_request_payload(
            context=self._prompt_context(active_mode),
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
            mode=active_mode,
        )

    def estimate_request_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request, self.context_state)

    def update_context_usage(self, request: ChatRequest, usage: MessageUsage) -> None:
        if usage.input_tokens + usage.output_tokens > 0:
            update_usage_anchor(self.context_state, request, usage)
        else:
            self.context_state.usage_anchor = None
        self._mark_dirty()

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
            result = await offload_tool_results(self.transcript, working_state, self._cwd)
            if result.transcript != self._transcript or working_state != self.context_state:
                self._transcript = result.transcript
                self._state.context_management = working_state
                self._mark_dirty()
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
            if persist and self._active_session_name is not None:
                try:
                    await asyncio.to_thread(self._write_active_session)
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

    def list_saved_sessions(self) -> list[StoredSessionInfo]:
        return self._session_store.list_sessions()

    def save_session(self, name: str) -> None:
        normalized = self._session_store.validate_name(name)
        if self._active_session_name != normalized and self._session_store.exists(normalized):
            raise SessionStoreError(f"会话名称已存在：{normalized}")
        if self._active_session_name is None:
            self._session_created_at = utc_now()
        self._active_session_name = normalized
        self._write_active_session()

    def auto_save(self) -> str | None:
        if self._active_session_name is None or not self._dirty:
            return None
        try:
            self._write_active_session()
        except SessionStoreError as exc:
            return str(exc)
        return None

    def remove_session(self, name: str) -> None:
        normalized = self._session_store.validate_name(name)
        if normalized == self._active_session_name:
            raise SessionStoreError("不能删除当前正在使用的会话。")
        self._session_store.remove(normalized)

    def rename_session(self, old_name: str, new_name: str) -> None:
        old_normalized = self._session_store.validate_name(old_name)
        new_normalized = self._session_store.validate_name(new_name)
        self._session_store.rename(old_normalized, new_normalized)
        if self._active_session_name == old_normalized:
            self._active_session_name = new_normalized
            self._write_active_session()

    def resume_session(self, name: str, *, force: bool = False) -> int:
        normalized = self._session_store.validate_name(name)
        if self._dirty and not force:
            raise SessionStoreError(
                "当前对话存在未保存改动；请先保存，或使用 /session resume <名称> --force。"
            )
        records = self._session_store.load(normalized)
        state, transcript, created_at, permission_rules = self._decode_records(records, normalized)
        for replacement in state.context_management.replacements.values():
            path = (self._cwd / replacement.relative_path).resolve()
            if self._cwd not in path.parents or not path.is_file():
                logger.warning(
                    "event=context_tool_result_missing context_id=%s call_id=%s path=%s",
                    state.context_management.context_id,
                    replacement.call_id,
                    replacement.relative_path,
                )
        self._permission_storage.replace_session_rules(permission_rules, notify=False)
        self._state = state
        self._transcript = transcript
        self._active_session_name = normalized
        self._session_created_at = created_at
        self._dirty = False
        return len(permission_rules)

    def _write_active_session(self) -> None:
        assert self._active_session_name is not None
        created_at = self._session_created_at or utc_now()
        self._session_created_at = created_at
        now = utc_now()
        records: list[dict[str, object]] = [
            {
                "type": "metadata",
                "version": SESSION_FORMAT_VERSION,
                "name": self._active_session_name,
                "project_root": str(self._cwd),
                "created_at": created_at.isoformat(),
                "updated_at": now.isoformat(),
                "message_count": len(self._state.messages),
                "permission_rule_count": len(self._permission_storage.rules_for_scope("session")),
                "context_management": self._encode_context_management(),
            },
            {
                "type": "state",
                "data": {
                    "runtime_mode": self._state.runtime_mode,
                    "previous_runtime_mode": self._state.previous_runtime_mode,
                    "plan_restore_mode": self._state.plan_restore_mode,
                    "plan_mode_turn_count": self._state.plan_mode_turn_count,
                    "pending_plan_exit_notice": self._state.pending_plan_exit_notice,
                    "pending_plan_entry_kind": self._state.pending_plan_entry_kind,
                },
            },
            {
                "type": "permissions",
                "data": {
                    "rules": [
                        {"match": rule.match, "result": rule.result}
                        for rule in self._permission_storage.rules_for_scope("session")
                    ]
                },
            },
        ]
        for message in self._state.messages:
            data = asdict(message)
            data["timestamp"] = message.timestamp.isoformat()
            records.append({"type": "message", "data": data})
        for message in self._transcript:
            records.append({"type": "transcript", "data": asdict(message)})
        self._session_store.save(self._active_session_name, records)
        self._dirty = False

    def _decode_records(
        self, records: list[dict[str, object]], expected_name: str
    ) -> tuple[SessionState, list[ConversationMessage], datetime, list[PermissionRule]]:
        try:
            allowed_types = {"metadata", "state", "permissions", "message", "transcript"}
            if any(record.get("type") not in allowed_types for record in records):
                raise SessionStoreError("会话文件包含未知记录类型。")
            if sum(record.get("type") == "state" for record in records) != 1:
                raise SessionStoreError("会话文件必须包含且仅包含一条 state 记录。")
            metadata = records[0]
            version = metadata.get("version")
            if metadata.get("type") != "metadata" or version not in SUPPORTED_SESSION_FORMAT_VERSIONS:
                raise SessionStoreError("不支持的会话文件格式。")
            if metadata.get("name") != expected_name:
                raise SessionStoreError("会话文件名称与 metadata 不一致。")
            if Path(str(metadata["project_root"])).resolve() != self._cwd:
                raise SessionStoreError("该会话不属于当前项目。")
            created_at = datetime.fromisoformat(str(metadata["created_at"]))
            state_record = next(record for record in records if record.get("type") == "state")
            state_data = state_record["data"]
            if not isinstance(state_data, dict):
                raise TypeError("state data")
            messages = [self._decode_message(record["data"]) for record in records if record.get("type") == "message"]
            transcript = [self._decode_transcript(record["data"]) for record in records if record.get("type") == "transcript"]
            permission_records = [record for record in records if record.get("type") == "permissions"]
            if version in {2, 3} and len(permission_records) != 1:
                raise SessionStoreError("v2/v3 会话必须包含且仅包含一条 permissions 记录。")
            if version == 1 and permission_records:
                raise SessionStoreError("v1 会话不能包含 permissions 记录。")
            permission_rules = (
                self._decode_permission_rules(permission_records[0]["data"])
                if permission_records
                else []
            )
            state = SessionState(
                messages=messages,
                runtime_mode=str(state_data.get("runtime_mode", "default")),  # type: ignore[arg-type]
                previous_runtime_mode=state_data.get("previous_runtime_mode"),  # type: ignore[arg-type]
                plan_restore_mode=str(state_data.get("plan_restore_mode", "default")),  # type: ignore[arg-type]
                plan_mode_turn_count=int(state_data.get("plan_mode_turn_count", 0)),
                pending_plan_exit_notice=bool(state_data.get("pending_plan_exit_notice", False)),
                pending_plan_entry_kind=state_data.get("pending_plan_entry_kind"),  # type: ignore[arg-type]
                context_management=(
                    self._decode_context_management(metadata.get("context_management"))
                    if version == 3
                    else ContextManagementState()
                ),
            )
            if state.runtime_mode not in {"default", "plan", "acceptEdits", "bypass"}:
                raise SessionStoreError("会话运行模式无效。")
            if len(messages) != int(metadata.get("message_count", -1)):
                raise SessionStoreError("会话消息数量与 metadata 不一致。")
            if len(permission_rules) != int(metadata.get("permission_rule_count", 0)):
                raise SessionStoreError("权限规则数量与 metadata 不一致。")
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise SessionStoreError(f"会话文件结构无效：{exc}") from exc
        return state, transcript, created_at, permission_rules

    def _encode_context_management(self) -> dict[str, object]:
        context = self.context_state
        return {
            "version": 1,
            "context_id": context.context_id,
            "usage_anchor": asdict(context.usage_anchor) if context.usage_anchor else None,
            "seen_call_ids": sorted(context.seen_call_ids),
            "replacements": {key: asdict(value) for key, value in context.replacements.items()},
            "recent_files": [asdict(value) for value in context.recent_files],
            "automatic_failure_count": context.automatic_failure_count,
            "automatic_compaction_disabled": context.automatic_compaction_disabled,
        }

    @staticmethod
    def _decode_context_management(value: object) -> ContextManagementState:
        if not isinstance(value, dict) or value.get("version") != 1:
            raise TypeError("context_management metadata")
        context_id = value.get("context_id")
        if not isinstance(context_id, str) or not context_id.strip():
            raise ValueError("context_id 无效。")
        anchor_data = value.get("usage_anchor")
        anchor = None
        if anchor_data is not None:
            if not isinstance(anchor_data, dict):
                raise TypeError("usage_anchor")
            anchor = ContextUsageAnchor(**anchor_data)
        raw_seen = value.get("seen_call_ids", [])
        raw_replacements = value.get("replacements", {})
        raw_files = value.get("recent_files", [])
        if not isinstance(raw_seen, list) or not all(isinstance(item, str) for item in raw_seen):
            raise TypeError("seen_call_ids")
        if not isinstance(raw_replacements, dict) or not isinstance(raw_files, list):
            raise TypeError("context management collections")
        replacements: dict[str, ToolResultReplacement] = {}
        for key, item in raw_replacements.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                raise TypeError("tool result replacement")
            replacements[key] = ToolResultReplacement(**item)
        files = [ContextFileSnapshot(**item) for item in raw_files if isinstance(item, dict)]
        return ContextManagementState(
            context_id=context_id,
            usage_anchor=anchor,
            seen_call_ids=set(raw_seen),
            replacements=replacements,
            recent_files=files,
            automatic_failure_count=int(value.get("automatic_failure_count", 0)),
            automatic_compaction_disabled=bool(value.get("automatic_compaction_disabled", False)),
        )

    @staticmethod
    def _decode_permission_rules(value: object) -> list[PermissionRule]:
        if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
            raise TypeError("permissions data")
        rules: list[PermissionRule] = []
        for item in value["rules"]:
            if not isinstance(item, dict):
                raise TypeError("permission rule")
            match = item.get("match")
            result = item.get("result")
            if not isinstance(match, str) or not match.strip():
                raise ValueError("权限规则 match 无效。")
            if result not in {"allow", "deny"}:
                raise ValueError("权限规则 result 无效。")
            rules.append(
                PermissionRule(match=match.strip(), result=result, scope="session")  # type: ignore[arg-type]
            )
        return rules

    @staticmethod
    def _decode_message(value: object) -> SessionMessage:
        if not isinstance(value, dict):
            raise TypeError("message data")
        usage = value.get("usage", {})
        trace = value.get("trace", {})
        if not isinstance(usage, dict) or not isinstance(trace, dict):
            raise TypeError("message fields")
        entries = trace.get("entries", [])
        if not isinstance(entries, list):
            raise TypeError("trace entries")
        return SessionMessage(
            id=str(value["id"]),
            role=str(value["role"]),  # type: ignore[arg-type]
            content=str(value.get("content", "")),
            status=str(value["status"]),  # type: ignore[arg-type]
            timestamp=datetime.fromisoformat(str(value["timestamp"])),
            usage=MessageUsage(**usage),
            trace=ThinkingTrace(
                entries=[TraceEntry(**entry) for entry in entries if isinstance(entry, dict)],
                collapsed=bool(trace.get("collapsed", True)),
            ),
        )

    @staticmethod
    def _decode_transcript(value: object) -> ConversationMessage:
        if not isinstance(value, dict) or not isinstance(value.get("blocks"), list):
            raise TypeError("transcript data")
        return ConversationMessage(
            role=str(value["role"]),  # type: ignore[arg-type]
            blocks=[ContentBlock(**block) for block in value["blocks"] if isinstance(block, dict)],
        )

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

    def _resolve_plan_file_path(self, plan_file_path: Path | None) -> Path:
        raw_path = plan_file_path or Path("./.lancher/plan.md")
        if not raw_path.is_absolute():
            raw_path = self._cwd / raw_path
        return raw_path.resolve()

    def _prompt_context(self, mode: RuntimeMode) -> "PromptContext":
        return build_prompt_context(
            cwd=self._cwd,
            current_date=self._current_date,
            runtime_mode=mode,
            plan_file_path=self._plan_file_path,
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
        if self._state.runtime_mode == "plan":
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
