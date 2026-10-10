from __future__ import annotations

import copy
import json
from dataclasses import asdict
from datetime import datetime

from lancher_code.context.models import (
    CompactionActivity,
    ContextFileSnapshot,
    ContextManagementState,
    ContextUsageAnchor,
)
from lancher_code.context.prefix import validate_prefix_state, validate_prefix_transcript
from lancher_code.contracts.messages import ContentBlock, ConversationMessage
from lancher_code.permissions.models import PermissionRule
from lancher_code.sessions.models import (
    PendingInput,
    PlanSnapshot,
    SessionMessage,
    SessionState,
    ThinkingTrace,
    TraceEntry,
)
from lancher_code.sessions.projection import project_events
from lancher_code.sessions.storage import SessionRepositoryError, reject_json_constant, unique_json_object
from lancher_code.usage.ledger import RequestUsageRecord
from lancher_code.usage.models import MessageUsage


class SessionCodec:
    """只处理新格式的状态编码与事件投影，不接触磁盘和模型供应商。"""

    @classmethod
    def encode(cls, state, transcript, rules, model_ref):
        state_data = {
            'work_phase': state.work_phase, 'permission_policy': state.permission_policy,
            'plan_mode_turn_count': state.plan_mode_turn_count,
            'pending_plan_exit_notice': state.pending_plan_exit_notice,
            'pending_plan_entry_kind': state.pending_plan_entry_kind,
            'plan_snapshot': asdict(state.plan_snapshot) if state.plan_snapshot else None,
            'pending_inputs': [asdict(item) for item in state.pending_inputs],
            'context_management': cls._encode_context_management(state.context_management),
            'execution': copy.deepcopy(state.execution),
            'request_usage': copy.deepcopy(state.request_usage),
            'compaction_activities': {key: cls._encode_compaction(item)
                                      for key, item in state.compaction_activities.items()},
        }
        messages = []
        for message in state.messages:
            data = asdict(message)
            data['usage'] = message.usage.to_dict()
            data['timestamp'] = message.timestamp.isoformat()
            messages.append(data)
        return {'state': state_data, 'messages': messages,
                'transcript': [asdict(item) for item in transcript],
                'rules': [asdict(rule) for rule in rules], 'model_ref': model_ref}

    @classmethod
    def decode(cls, data, session_id):
        try:
            raw = data['state']
            if 'request_usage' not in raw:
                raise ValueError('会话缺少请求用量账本，旧计量格式不兼容，请创建新会话。')
            phase, policy = raw["work_phase"], raw["permission_policy"]
            if phase not in {"discuss", "plan", "execute"} or policy not in {"default", "acceptEdits", "bypass"}:
                raise ValueError("工作阶段或权限策略无效。")
            if raw['pending_plan_entry_kind'] not in {None, 'initial', 'reentry'}:
                raise ValueError('计划提示状态无效。')
            if type(raw['plan_mode_turn_count']) is not int or raw['plan_mode_turn_count'] < 0 or type(raw['pending_plan_exit_notice']) is not bool:
                raise ValueError('计划回合状态无效。')
            state = SessionState(
                session_id=session_id, work_phase=phase, permission_policy=policy,
                messages=[cls._decode_message(item) for item in data['messages']],
                plan_snapshot=cls.decode_plan_snapshot(raw['plan_snapshot']),
                pending_inputs=cls.decode_pending_inputs(raw['pending_inputs']),
                plan_mode_turn_count=raw['plan_mode_turn_count'],
                pending_plan_exit_notice=raw['pending_plan_exit_notice'],
                pending_plan_entry_kind=raw['pending_plan_entry_kind'],
                context_management=cls._decode_context_management(raw['context_management']),
                execution=cls._decode_execution(raw['execution']),
                request_usage=cls._decode_request_usage(raw['request_usage'], session_id),
                compaction_activities=cls._decode_compactions(raw['compaction_activities']),
            )
            if len({item.id for item in state.messages}) != len(state.messages):
                raise ValueError("消息 id 重复。")
            known_messages = {item.id: item for item in state.messages}
            for activity in state.compaction_activities.values():
                if activity.message_id is not None:
                    owner = known_messages.get(activity.message_id)
                    if owner is None or owner.role != 'assistant':
                        raise ValueError('压缩活动关联的助手消息无效。')
                if activity.after_message_id is not None and activity.after_message_id not in known_messages:
                    raise ValueError('压缩活动的对话位置无效。')
            for message in state.messages:
                for entry in message.trace.entries:
                    if entry.kind == 'compaction':
                        activity = state.compaction_activities.get(entry.metadata.get('activity_id'))
                        if activity is None or activity.message_id != message.id:
                            raise ValueError('压缩活动轨迹关联无效。')
            transcript = [cls.decode_transcript(item) for item in data['transcript']]
            validate_prefix_transcript(state.context_management.prefix_state, transcript)
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
            return project_events(events, decode_compaction=cls._decode_compaction,
                                  apply_execution_event=cls.apply_execution_event)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise SessionRepositoryError(f'会话事件内容无效：{exc}') from exc

    @staticmethod
    def _encode_compaction(activity):
        data = asdict(activity)
        data['started_at'] = activity.started_at.isoformat()
        data['finished_at'] = activity.finished_at.isoformat() if activity.finished_at else None
        return data

    @classmethod
    def _decode_compactions(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError('压缩活动必须为对象。')
        result = {}
        for activity_id, data in raw.items():
            activity = cls._decode_compaction(data)
            if activity_id != activity.id:
                raise ValueError('压缩活动 ID 与存储键不一致。')
            result[activity_id] = activity
        return result

    @staticmethod
    def _decode_compaction(raw):
        if not isinstance(raw, dict):
            raise ValueError('压缩活动必须为对象。')
        data = copy.deepcopy(raw)
        for name in ('id', 'message_id', 'after_message_id', 'turn_id'):
            value = data.get(name)
            if (name == 'id' or value is not None) and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'压缩活动 {name} 无效。')
        if data.get('trigger') not in {'manual', 'automatic', 'emergency'}:
            raise ValueError('压缩触发方式无效。')
        if data.get('status') not in {'running', 'completed', 'failed', 'cancelled', 'interrupted'}:
            raise ValueError('压缩活动状态无效。')
        for name in ('started_at', 'finished_at'):
            value = data.get(name)
            if name == 'finished_at' and value is None:
                continue
            if not isinstance(value, str):
                raise ValueError('压缩活动时间必须为字符串。')
            data[name] = datetime.fromisoformat(value)
            if data[name].tzinfo is None:
                raise ValueError('压缩活动时间必须包含时区。')
        if ((data['status'] == 'running' and data.get('finished_at') is not None)
                or (data['status'] not in {'running', 'interrupted'} and data.get('finished_at') is None)
                or (data.get('finished_at') is not None and data['finished_at'] < data['started_at'])):
            raise ValueError('压缩活动开始与结束状态不一致。')
        for prefix in ('before', 'after'):
            tokens, source = data.get(f'{prefix}_tokens'), data.get(f'{prefix}_source')
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError('压缩活动估算必须为非负整数。')
            if source not in {None, 'estimated', 'usage_calibrated'} or (tokens is None) != (source is None):
                raise ValueError('压缩活动计数与估算来源不一致。')
        if data['status'] == 'completed' and (data.get('before_tokens') is None or data.get('after_tokens') is None):
            raise ValueError('完成的压缩活动缺少前后估算。')
        if data['status'] != 'completed' and data.get('after_tokens') is not None:
            raise ValueError('未完成的压缩活动不能记录成功后的估算。')
        if type(data.get('dropped_groups')) is not int or data['dropped_groups'] < 0:
            raise ValueError('压缩活动省略组数无效。')
        if type(data.get('continued')) is not bool:
            raise ValueError('压缩活动继续标记无效。')
        if data['continued'] and (data['status'] != 'failed' or data['trigger'] == 'manual'):
            raise ValueError('仅自动压缩失败可以继续本轮。')
        if data.get('error_text') is not None and not isinstance(data['error_text'], str):
            raise ValueError('压缩活动失败说明必须为字符串。')
        return CompactionActivity(**data)

    @staticmethod
    def _decode_request_usage(raw, session_id):
        if not isinstance(raw, dict):
            raise ValueError('请求用量必须为对象。')
        records = {}
        for request_id, data in raw.items():
            record = RequestUsageRecord.from_dict(data)
            if record.request_id != request_id or record.session_id != session_id:
                raise ValueError('请求用量 ID 或 Session 归属不一致。')
            records[request_id] = record.to_dict()
        return records

    @staticmethod
    def _decode_execution(raw):
        if not isinstance(raw, dict) or any(key not in raw for key in ('processes', 'invocations', 'inbox')):
            raise ValueError('执行状态格式无效。')
        if not isinstance(raw['processes'], dict) or not isinstance(raw['invocations'], dict) or not isinstance(raw['inbox'], list):
            raise ValueError('执行状态集合格式无效。')
        for key, value in raw['processes'].items():
            if not isinstance(value, dict) or value.get('process_id') != key or not isinstance(value.get('status'), str):
                raise ValueError('进程记录格式无效。')
        for key, value in raw['invocations'].items():
            if not isinstance(value, dict) or value.get('invocation_id') != key or not isinstance(value.get('state'), str):
                raise ValueError('调用记录格式无效。')
        if any(not isinstance(item, dict) or not isinstance(item.get('notification_id'), str) for item in raw['inbox']):
            raise ValueError('执行通知格式无效。')
        return copy.deepcopy(raw)

    @staticmethod
    def apply_execution_event(execution, kind, data):
        if kind.startswith('process.'):
            info = copy.deepcopy(data)
            notification_id = info.pop('notification_id', None)
            execution['processes'][info['process_id']] = info
            if kind == 'process.exited' and notification_id is not None:
                if not any(item['notification_id'] == notification_id for item in execution['inbox']):
                    execution['inbox'].append(dict(info, notification_id=notification_id))
        elif kind.startswith('invocation.'):
            execution['invocations'][data['invocation_id']] = copy.deepcopy(data)
        elif kind == 'execution.inbox_acknowledged':
            acknowledged = set(data['notification_ids'])
            execution['inbox'][:] = [item for item in execution['inbox'] if item['notification_id'] not in acknowledged]
        else:
            raise ValueError(f'未知执行事件：{kind}')

    @staticmethod
    def decode_plan_snapshot(value: object) -> PlanSnapshot | None:
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
    def decode_pending_inputs(value: object) -> list[PendingInput]:
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
            items.append(PendingInput(item_id, text, delivery, target, state))
        return items

    @staticmethod
    def _encode_context_management(context: ContextManagementState) -> dict[str, object]:
        return {
            "version": 2,
            "context_id": context.context_id,
            "usage_anchor": asdict(context.usage_anchor) if context.usage_anchor else None,
            "replacements": dict(context.replacements),
            "recent_files": [asdict(value) for value in context.recent_files],
            "automatic_failure_count": context.automatic_failure_count,
            "automatic_compaction_disabled": context.automatic_compaction_disabled,
            "skill_activations": copy.deepcopy(context.skill_activations),
            "disabled_skills": list(context.disabled_skills),
            "prefix_state": copy.deepcopy(context.prefix_state),
            "frozen_tool_previews": dict(context.frozen_tool_previews),
        }

    @staticmethod
    def _decode_context_management(value: object) -> ContextManagementState:
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 2:
            raise ValueError("上下文计量格式无效或不兼容，请创建新会话。")
        context_id = value["context_id"]
        if not isinstance(context_id, str) or not context_id.strip():
            raise ValueError("context_id 无效。")
        anchor_data = value["usage_anchor"]
        anchor = None
        if anchor_data is not None:
            if not isinstance(anchor_data, dict):
                raise TypeError("usage_anchor")
            anchor = ContextUsageAnchor(**anchor_data)
            for name in ("token_count", "message_count"):
                count = getattr(anchor, name)
                # bool 是 int 的子类，但 true 不能充当服务端输入计数。
                if type(count) is not int or count < 0:
                    raise ValueError(f"usage_anchor.{name} 必须为非负整数。")
            for name in ("system_tools_digest", "messages_digest"):
                digest = getattr(anchor, name)
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(character not in "0123456789abcdef" for character in digest)):
                    raise ValueError(f"usage_anchor.{name} 必须为 SHA-256 摘要。")
        failure_count = value["automatic_failure_count"]
        disabled = value["automatic_compaction_disabled"]
        if type(failure_count) is not int or failure_count < 0:
            raise ValueError("automatic_failure_count 必须为非负整数。")
        if type(disabled) is not bool:
            raise ValueError("automatic_compaction_disabled 必须为布尔值。")
        raw_replacements = value["replacements"]
        raw_files = value["recent_files"]
        if not isinstance(raw_replacements, dict) or not isinstance(raw_files, list):
            raise TypeError("context management collections")
        if any(not isinstance(key, str) or not key or not isinstance(path, str) or not path
               for key, path in raw_replacements.items()):
            raise ValueError("工具结果引用必须包含调用标识和相对路径。")
        if any(not isinstance(item, dict) for item in raw_files):
            raise TypeError("context file snapshot")
        files = [ContextFileSnapshot(**item) for item in raw_files]
        activations = value.get('skill_activations', {})
        disabled_skills = value.get('disabled_skills', [])
        prefix = validate_prefix_state(value.get('prefix_state', {}))
        previews = value.get('frozen_tool_previews', {})
        if (not isinstance(previews, dict) or any(not isinstance(key, str) or not key
                or rendered is not None and not isinstance(rendered, str) for key, rendered in previews.items())):
            raise ValueError('冻结工具预览无效。')
        if not isinstance(activations, dict) or len(activations) > 128:
            raise ValueError('技能激活记录无效。')
        for skill_id, item in activations.items():
            if not isinstance(skill_id, str) or not isinstance(item, dict) or item.get('id') != skill_id:
                raise ValueError('技能激活身份无效。')
            for key in ('name', 'description', 'scope', 'path', 'directory', 'digest', 'body', 'activation_kind'):
                if not isinstance(item.get(key), str):
                    raise ValueError(f'技能激活字段 {key} 无效。')
            if (type(item.get('loaded')) is not bool or item['scope'] not in {'project', 'user'}
                    or item['activation_kind'] not in {'explicit', 'automatic'}
                    or skill_id != f"{item['scope']}/{item['name']}"
                    or len(item['body']) > 24_000 or (not item['loaded'] and item['body'])):
                raise ValueError('技能激活状态无效。')
            if len(item['digest']) != 64 or any(c not in '0123456789abcdef' for c in item['digest']):
                raise ValueError('技能内容指纹无效。')
        if (not isinstance(disabled_skills, list) or len(disabled_skills) > 128
                or any(not isinstance(item, str) or not item for item in disabled_skills)):
            raise ValueError('技能禁用记录无效。')
        return ContextManagementState(
            context_id=context_id,
            usage_anchor=anchor,
            replacements=dict(raw_replacements),
            recent_files=files,
            automatic_failure_count=failure_count,
            automatic_compaction_disabled=disabled,
            skill_activations=copy.deepcopy(activations),
            disabled_skills=list(dict.fromkeys(disabled_skills)),
            prefix_state=prefix,
            frozen_tool_previews=dict(previews),
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
            if match_kind not in {"exact", "glob"}:
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
        decoded_usage = MessageUsage.from_dict(usage)
        if type(trace['collapsed']) is not bool:
            raise ValueError('消息轨迹状态无效。')
        entries = []
        for entry in trace['entries']:
            if not isinstance(entry, dict) or entry.get('kind') not in {'thinking', 'text', 'notice', 'tool_call', 'tool_result', 'compaction'}:
                raise ValueError('消息轨迹条目无效。')
            if not isinstance(entry.get('metadata'), dict) or not isinstance(entry.get('arguments'), dict):
                raise ValueError('消息轨迹 metadata 和 arguments 必须是对象。')
            if any(not isinstance(entry.get(key), str) for key in ('text', 'call_id', 'tool_name')):
                raise ValueError('消息轨迹文本无效。')
            entries.append(TraceEntry(**entry))
        return SessionMessage(id=value['id'], role=value['role'], content=value['content'],
                              status=value['status'], timestamp=timestamp, usage=decoded_usage,
                              trace=ThinkingTrace(entries=entries, collapsed=trace['collapsed']))

    @staticmethod
    def decode_transcript(value: object) -> ConversationMessage:
        if not isinstance(value, dict) or not isinstance(value.get('blocks'), list):
            raise ValueError('模型上下文消息必须包含 blocks 数组。')
        if value['role'] not in {'system', 'user', 'assistant', 'tool'}:
            raise ValueError('模型上下文角色无效。')
        response_protocol, response_model = value.get('response_protocol'), value.get('response_model')
        if response_protocol is not None and (
            not isinstance(response_protocol, str) or response_protocol not in {'claude', 'openai'}
        ):
            raise ValueError('助手响应来源协议无效。')
        if response_model is not None and (not isinstance(response_model, str) or not response_model.strip()):
            raise ValueError('助手响应来源模型无效。')
        if value['role'] != 'assistant' and (response_protocol is not None or response_model is not None):
            raise ValueError('响应来源只能属于助手消息。')
        if value['role'] == 'assistant' and any(
            isinstance(block, dict) and block.get('kind') in {'thinking', 'redacted_thinking', 'tool_use'}
            for block in value['blocks']
        ) and (response_protocol is None or response_model is None):
            raise ValueError('助手协议响应缺少完整来源，请创建新会话。')
        blocks = []
        for raw in value['blocks']:
            if not isinstance(raw, dict) or raw.get('kind') not in {'text', 'thinking', 'redacted_thinking', 'tool_use', 'tool_result'}:
                raise ValueError('模型上下文内容块无效。')
            block = ContentBlock(**raw)
            if not isinstance(block.text, str) or not isinstance(block.input, dict) or type(block.is_error) is not bool:
                raise ValueError('模型上下文内容块字段无效。')
            if block.kind in {'tool_use', 'tool_result'} and (not isinstance(block.call_id, str) or not block.call_id):
                raise ValueError('工具内容块缺少调用标识。')
            if any(item is not None and not isinstance(item, str) for item in (block.signature, block.data)):
                raise ValueError('思考签名或密文必须为文本。')
            if block.thinking_protocol is not None and (
                not isinstance(block.thinking_protocol, str) or block.thinking_protocol not in {'claude', 'openai'}
            ):
                raise ValueError('思考内容块协议无效。')
            if block.thinking_field is not None and (
                not isinstance(block.thinking_field, str) or block.thinking_field not in {'reasoning_content', 'reasoning'}
            ):
                raise ValueError('思考内容块字段来源无效。')
            if block.kind in {'thinking', 'redacted_thinking'}:
                if value['role'] != 'assistant':
                    raise ValueError('思考内容块只能属于助手消息。')
                if block.thinking_field is not None and block.thinking_protocol != 'openai':
                    raise ValueError('推理字段来源必须属于 OpenAI 协议。')
                if block.kind == 'thinking':
                    # 某些提供方不给可读思考，只返回可验证签名。这种真实
                    # 内容仍需原样保存；单纯空块则不能冒充完整协议。
                    if not block.text.strip() and not block.signature and block.data is None:
                        raise ValueError('思考内容与签名不能同时为空。')
                    if block.data is not None:
                        if block.thinking_protocol != 'openai' or block.signature is not None or block.thinking_field is not None:
                            raise ValueError('opaque reasoning 必须属于独立 OpenAI Responses 内容块。')
                        try:
                            opaque = json.loads(block.data, parse_constant=reject_json_constant, object_pairs_hook=unique_json_object)
                        except (ValueError, TypeError) as exc:
                            raise ValueError('opaque reasoning 不是有效 JSON。') from exc
                        if (not isinstance(opaque, dict) or opaque.get('type') != 'reasoning'
                                or not isinstance(opaque.get('id'), str) or not opaque['id']
                                or not isinstance(opaque.get('summary'), list)
                                or any(not isinstance(item, dict) or item.get('type') != 'summary_text'
                                       or not isinstance(item.get('text'), str) for item in opaque['summary'])
                                or opaque.get('encrypted_content') is not None
                                and (not isinstance(opaque['encrypted_content'], str) or not opaque['encrypted_content'])):
                            raise ValueError('opaque reasoning 内容或来源字段无效。')
                elif not block.data or block.text or block.signature is not None or block.thinking_field is not None:
                    raise ValueError('屏蔽思考必须包含独立的非空密文。')
            elif any(item is not None for item in (
                block.signature, block.data, block.thinking_protocol, block.thinking_field,
            )):
                raise ValueError('普通内容块不能携带思考协议字段。')
            blocks.append(block)
        return ConversationMessage(role=value['role'], blocks=blocks,
                                   response_protocol=response_protocol, response_model=response_model)
