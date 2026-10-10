"""仅重放当前格式事件，不接触磁盘或迁移旧数据。"""
from __future__ import annotations

import copy
from dataclasses import asdict

from lancher_code.sessions.models import TraceEntry
from lancher_code.sessions.storage import SessionRepositoryError


def project_events(events, *, decode_compaction, apply_execution_event):
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
        elif kind == 'context.prefix_updated':
            if type(data.get('replace')) is not bool:
                raise ValueError('固定前缀提交缺少替换标记。')
            if data['replace']:
                result['transcript'] = copy.deepcopy(data['messages'])
            else:
                result['transcript'].extend(copy.deepcopy(data['messages']))
            result['state']['context_management'] = copy.deepcopy(data['context_management'])
        elif kind == 'transcript.updated':
            result['transcript'][data['index']] = copy.deepcopy(data['message'])
        elif kind in {'context.compacted', 'context.replaced'}:
            result['transcript'] = copy.deepcopy(data['messages'])
            if kind == 'context.compacted':
                activity = decode_compaction(data['compaction'])
                if activity.id != data['activity_id'] or activity.status != 'completed':
                    raise ValueError('上下文压缩提交必须关联同一活动的完成快照。')
                result['state']['compaction_activities'][activity.id] = copy.deepcopy(data['compaction'])
                result['state']['context_management'] = copy.deepcopy(data['context_management'])
        elif kind == 'state.changed':
            # 其它状态变更不会携带累计账本，账本仅由请求增量事件更新。
            if 'request_usage' not in result['state']:
                raise SessionRepositoryError('会话缺少请求用量账本，不能恢复未知的历史消耗。')
            request_usage = result['state']['request_usage']
            activities = result['state']['compaction_activities']
            result['state'] = copy.deepcopy(data)
            result['state']['request_usage'] = request_usage
            result['state']['compaction_activities'] = activities
        elif kind == 'permissions.changed':
            result['rules'] = copy.deepcopy(data['rules'])
        elif kind == 'model.changed':
            result['model_ref'] = data['model_ref']
        elif kind.startswith('process.') or kind.startswith('invocation.') or kind == 'execution.inbox_acknowledged':
            execution = result['state']['execution']
            apply_execution_event(execution, kind, data)
        elif kind == 'usage.request_updated':
            if 'request_usage' not in result['state']:
                raise SessionRepositoryError('会话缺少请求用量账本，不能恢复未知的历史消耗。')
            result['state']['request_usage'][data['request_id']] = copy.deepcopy(data)
        elif kind == 'compaction.updated':
            activity = decode_compaction(data)
            result['state']['compaction_activities'][activity.id] = copy.deepcopy(data)
            if activity.message_id is not None:
                owner = messages.get(activity.message_id)
                if owner is None or owner['role'] != 'assistant':
                    raise ValueError('压缩活动关联的助手消息无效。')
                entries = owner['trace']['entries']
                if not any(entry['kind'] == 'compaction' and entry['metadata'].get('activity_id') == activity.id
                           for entry in entries):
                    # 开始事件本身就固定活动位置；随后 message.updated
                    # 尚未落盘也能恢复卡片，终态事件则不会重复追加。
                    if entries:
                        last = entries[-1]
                        if last['kind'] in {'text', 'thinking'} and last['metadata'].get('state') == 'streaming':
                            last['metadata']['state'] = 'complete'
                    elif owner['status'] == 'streaming':
                        owner['trace']['collapsed'] = False
                    entries.append(asdict(TraceEntry(kind='compaction', metadata={'activity_id': activity.id})))
        elif kind in {'session.renamed', 'session.archived', 'turn.started', 'turn.completed', 'turn.failed', 'turn.interrupted', 'tool.started', 'tool.finished'}:
            pass
        else:
            raise SessionRepositoryError(f'未知的会话事件：{kind}')
    return result
