from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime

from lancher_code.models import (
    SessionState, SessionMessage, ConversationMessage, ContentBlock, MessageUsage,
    ThinkingTrace, TraceEntry, PlanSnapshot, PendingInput, PermissionRule,
    ContextManagementState, ContextUsageAnchor, ContextFileSnapshot,
    ToolResultReplacement, resolve_runtime_axes,
)
from lancher_code.sessions.repository import SessionRepositoryError


class SessionCodec:
    """只处理新格式的状态编码与事件投影，不接触磁盘和模型供应商。"""

    @classmethod
    def encode(cls, state, transcript, rules, model_ref):
        state_data = {
            'work_phase': state.work_phase, 'permission_policy': state.permission_policy,
            'previous_runtime_mode': state.previous_runtime_mode,
            'plan_mode_turn_count': state.plan_mode_turn_count,
            'pending_plan_exit_notice': state.pending_plan_exit_notice,
            'pending_plan_entry_kind': state.pending_plan_entry_kind,
            'plan_snapshot': asdict(state.plan_snapshot) if state.plan_snapshot else None,
            'pending_inputs': [asdict(item) for item in state.pending_inputs],
            'context_management': cls._encode_context_management(state.context_management),
        }
        messages = []
        for message in state.messages:
            data = asdict(message)
            data['timestamp'] = message.timestamp.isoformat()
            messages.append(data)
        return {'state': state_data, 'messages': messages,
                'transcript': [asdict(item) for item in transcript],
                'rules': [asdict(rule) for rule in rules], 'model_ref': model_ref}

    @classmethod
    def decode(cls, data, session_id):
        try:
            raw = data['state']
            phase, policy = resolve_runtime_axes(work_phase=raw['work_phase'], permission_policy=raw['permission_policy'])
            if raw['previous_runtime_mode'] not in {None, 'default', 'plan', 'acceptEdits', 'bypass'}:
                raise ValueError('上一阶段状态无效。')
            if raw['pending_plan_entry_kind'] not in {None, 'initial', 'reentry'}:
                raise ValueError('计划提示状态无效。')
            if type(raw['plan_mode_turn_count']) is not int or raw['plan_mode_turn_count'] < 0 or type(raw['pending_plan_exit_notice']) is not bool:
                raise ValueError('计划回合状态无效。')
            state = SessionState(
                session_id=session_id, work_phase=phase, permission_policy=policy,
                messages=[cls._decode_message(item) for item in data['messages']],
                plan_snapshot=cls._decode_plan_snapshot(raw['plan_snapshot']),
                pending_inputs=cls._decode_pending_inputs(raw['pending_inputs'], restore=False),
                previous_runtime_mode=raw['previous_runtime_mode'],
                plan_mode_turn_count=raw['plan_mode_turn_count'],
                pending_plan_exit_notice=raw['pending_plan_exit_notice'],
                pending_plan_entry_kind=raw['pending_plan_entry_kind'],
                context_management=cls._decode_context_management(raw['context_management']),
            )
            if len({item.id for item in state.messages}) != len(state.messages):
                raise ValueError("消息 id 重复。")
            transcript = [cls._decode_transcript(item) for item in data['transcript']]
            rules = cls._decode_permission_rules({'rules': data['rules']})
            model_ref = data['model_ref']
            if model_ref is not None and (not isinstance(model_ref, str) or not model_ref.strip()):
                raise ValueError('模型引用无效。')
            return state, transcript, rules, model_ref
        except (KeyError, ValueError, TypeError) as exc:
            raise SessionRepositoryError(f'会话状态无效：{exc}') from exc

    @classmethod
    def project(cls, events):
        try:
            return cls._project(events)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SessionRepositoryError(f'会话事件内容无效：{exc}') from exc

    @staticmethod
    def _project(events):
        if not events or events[0]['type'] != 'session.created':
            raise SessionRepositoryError('会话缺少创建记录。')
        result = copy.deepcopy(events[0]['data']['initial_data'])
        messages = {item['id']: item for item in result['messages']}
        for event in events[1:]:
            kind, data = event['type'], event['data']
            if kind == 'message.created':
                if data['id'] in messages:
                    raise SessionRepositoryError('消息 id 重复。')
                item = copy.deepcopy(data)
                result['messages'].append(item)
                messages[item['id']] = item
            elif kind == 'message.updated':
                item = messages[data['id']]
                item.update(copy.deepcopy(data.get('fields', {})))
                if 'content_delta' in data:
                    item['content'] += data['content_delta']
                if 'content' in data:
                    item['content'] = data['content']
            elif kind == 'transcript.appended':
                result['transcript'].extend(copy.deepcopy(data['messages']))
            elif kind == 'transcript.updated':
                result['transcript'][data['index']] = copy.deepcopy(data['message'])
            elif kind in {'context.compacted', 'context.replaced'}:
                result['transcript'] = copy.deepcopy(data['messages'])
            elif kind == 'state.changed':
                result['state'] = copy.deepcopy(data)
            elif kind == 'permissions.changed':
                result['rules'] = copy.deepcopy(data['rules'])
            elif kind == 'model.changed':
                result['model_ref'] = data['model_ref']
            elif kind in {'session.renamed', 'session.archived', 'turn.started', 'turn.completed', 'turn.failed', 'turn.interrupted', 'tool.started', 'tool.finished'}:
                pass
            else:
                raise SessionRepositoryError(f'未知的会话事件：{kind}')
        return result

    @staticmethod
    def _decode_plan_snapshot(value: object) -> PlanSnapshot | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("计划快照必须为对象。")
        content, source = value.get("content"), value.get("source_message_id")
        ready = value.get("ready", False)
        if not isinstance(content, str) or not content.strip() or not isinstance(source, str) or not source.strip():
            raise ValueError("计划快照正文或来源消息无效。")
        if not isinstance(ready, bool):
            raise ValueError("计划快照 ready 必须为布尔值。")
        snapshot = PlanSnapshot.create(content, source, ready=ready)
        if value.get("digest") != snapshot.digest:
            raise ValueError("计划快照内容与摘要不一致。")
        return snapshot

    @staticmethod
    def _decode_pending_inputs(value: object, *, restore: bool) -> list[PendingInput]:
        if not isinstance(value, list):
            raise ValueError("待处理输入必须为数组。")
        items: list[PendingInput] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError("待处理输入格式无效。")
            item_id, text = raw.get("id"), raw.get("text")
            if not isinstance(item_id, str) or not item_id.strip() or item_id in seen:
                raise ValueError("待处理输入 id 为空或重复。")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("待处理输入正文无效。")
            delivery, state = raw.get("delivery", "follow_up"), raw.get("state", "pending")
            target = raw.get("target_task_id")
            if delivery not in {"follow_up", "steer"} or state not in {"pending", "paused"}:
                raise ValueError("待处理输入动作或状态无效。")
            if target is not None and (not isinstance(target, str) or not target.strip()):
                raise ValueError("待处理输入的目标任务无效。")
            seen.add(item_id)
            items.append(PendingInput(item_id, text, delivery, target, "paused" if restore else state))
        return items

    @staticmethod
    def _encode_context_management(context: ContextManagementState) -> dict[str, object]:
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
            match_kind = item["match_kind"]
            if match_kind not in {"exact", "glob", "legacy"}:
                raise ValueError("权限规则 match_kind 无效。")
            rules.append(
                PermissionRule(match=match.strip(), result=result, scope="session", match_kind=match_kind)
            )
        return rules

    @staticmethod
    def _decode_message(value: object) -> SessionMessage:
        if not isinstance(value, dict):
            raise ValueError('消息必须是对象。')
        if not isinstance(value['id'], str) or not value['id'].strip():
            raise ValueError('消息 id 无效。')
        if value['role'] not in {'system', 'user', 'assistant'} or value['status'] not in {'streaming', 'complete', 'error', 'cancelled'}:
            raise ValueError('消息角色或状态无效。')
        if not isinstance(value['content'], str):
            raise ValueError('消息正文必须是字符串。')
        timestamp = datetime.fromisoformat(value['timestamp'])
        if timestamp.tzinfo is None:
            raise ValueError('消息时间必须包含时区。')
        usage, trace = value['usage'], value['trace']
        if not isinstance(usage, dict) or not isinstance(trace, dict) or not isinstance(trace['entries'], list):
            raise ValueError('消息用量或轨迹无效。')
        if any(type(number) is not int or number < 0 for number in usage.values()):
            raise ValueError('消息用量必须是非负整数。')
        if type(trace['collapsed']) is not bool or type(value['timeline_version']) is not int:
            raise ValueError('消息轨迹状态无效。')
        entries = []
        for entry in trace['entries']:
            if not isinstance(entry, dict) or entry.get('kind') not in {'thinking', 'text', 'notice', 'tool_call', 'tool_result'}:
                raise ValueError('消息轨迹条目无效。')
            if not isinstance(entry.get('metadata'), dict) or not isinstance(entry.get('arguments'), dict):
                raise ValueError('消息轨迹 metadata 和 arguments 必须是对象。')
            if any(not isinstance(entry.get(key), str) for key in ('text', 'call_id', 'tool_name')):
                raise ValueError('消息轨迹文本无效。')
            entries.append(TraceEntry(**entry))
        return SessionMessage(id=value['id'], role=value['role'], content=value['content'],
                              status=value['status'], timestamp=timestamp, usage=MessageUsage(**usage),
                              timeline_version=value['timeline_version'],
                              trace=ThinkingTrace(entries=entries, collapsed=trace['collapsed']))

    @staticmethod
    def _decode_transcript(value: object) -> ConversationMessage:
        if not isinstance(value, dict) or not isinstance(value.get('blocks'), list):
            raise ValueError('模型上下文消息必须包含 blocks 数组。')
        if value['role'] not in {'system', 'user', 'assistant', 'tool'}:
            raise ValueError('模型上下文角色无效。')
        blocks = []
        for raw in value['blocks']:
            if not isinstance(raw, dict) or raw.get('kind') not in {'text', 'tool_use', 'tool_result'}:
                raise ValueError('模型上下文内容块无效。')
            block = ContentBlock(**raw)
            if not isinstance(block.text, str) or not isinstance(block.input, dict) or type(block.is_error) is not bool:
                raise ValueError('模型上下文内容块字段无效。')
            if block.kind != 'text' and (not isinstance(block.call_id, str) or not block.call_id):
                raise ValueError('工具内容块缺少调用标识。')
            blocks.append(block)
        return ConversationMessage(role=value['role'], blocks=blocks)
