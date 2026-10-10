from __future__ import annotations

import copy
from uuid import uuid4

from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.repository import ProjectSessionRepository
from lancher_code.sessions.storage import SessionRepositoryError


class SessionService:
    """协调会话身份、独占写入与增量持久化；运行时状态由 Controller 管理。"""

    def __init__(self, project_root):
        self.repository = ProjectSessionRepository(project_root)
        self.writer = None
        self.paths = None
        self.title = None
        self._saved = None

    def create(self, initial_data, first_input):
        session_id = uuid4().hex
        title = first_input.strip().splitlines()[0][:60]
        writer = self.repository.create(session_id, title, initial_data)
        self.writer, self.paths, self.title = writer, writer.paths, title
        self._saved = copy.deepcopy(initial_data)
        return session_id

    def prepare(self, session_id):
        writer = self.repository.open(session_id)
        try:
            events = self.repository.read(session_id)
            checkpoint = self.repository.load_checkpoint(session_id)
            if checkpoint is not None:
                try:
                    tail = [{'type': 'session.created', 'data': {'initial_data': checkpoint['state']}}]
                    tail.extend(events[checkpoint['last_seq']:])
                    snapshot = SessionCodec.project(tail)
                    decoded = SessionCodec.decode(snapshot, session_id)
                except SessionRepositoryError:
                    checkpoint = None
            if checkpoint is None:
                snapshot = SessionCodec.project(events)
                decoded = SessionCodec.decode(snapshot, session_id)
            title = events[0]['data']['title']
            for event in events:
                if event['type'] == 'session.renamed':
                    title = event['data']['title']
            return writer, snapshot, decoded, title
        except Exception:
            writer.close()
            raise

    def activate(self, prepared):
        writer, snapshot, _, title = prepared
        if self.writer is not None:
            self.writer.close()
        self.writer, self.paths, self.title = writer, writer.paths, title
        self._saved = copy.deepcopy(snapshot)

    def persist(self, snapshot, *, context_event='context.replaced', context_activity_id=None):
        if self.writer is None:
            return
        previous = self._saved
        # 请求账本只写单条增量。恢复中把 running 改为 incomplete 时，也
        # 必须经过同一个事件出口；正常回调已同步 _saved，不会重复追加。
        previous_requests = previous['state']['request_usage']
        current_requests = snapshot['state']['request_usage']
        if previous_requests.keys() - current_requests.keys():
            raise SessionRepositoryError('请求账本不能删除已保存的历史记录。')
        for request_id, record in current_requests.items():
            if previous_requests.get(request_id) != record:
                self.record_usage(record, turn_id=record.get('turn_id'))
        previous_activities = previous['state']['compaction_activities']
        current_activities = snapshot['state']['compaction_activities']
        if previous_activities.keys() - current_activities.keys():
            raise SessionRepositoryError('压缩活动不能删除已保存的历史记录。')
        completed_activity = None
        if context_activity_id is not None:
            completed_activity = current_activities.get(context_activity_id)
            if (context_event != 'context.compacted' or completed_activity is None
                    or completed_activity['status'] != 'completed'):
                raise SessionRepositoryError('上下文压缩提交缺少对应活动的完成快照。')
        for activity_id, activity in current_activities.items():
            # 成功不能先写成一条孤立活动事件：必须和实际上下文同时提交。
            if activity_id != context_activity_id and previous_activities.get(activity_id) != activity:
                self.record_compaction(activity)
        known = {message['id']: message for message in previous['messages']}
        for message in snapshot['messages']:
            old = known.get(message['id'])
            if old is None:
                self.writer.append('message.created', message)
                previous['messages'].append(copy.deepcopy(message))
                continue
            if old == message:
                continue
            data = {'id': message['id'], 'fields': {
                key: value for key, value in message.items()
                if key not in {'id', 'content'} and old.get(key) != value
            }}
            if old['content'] != message['content']:
                if message['content'].startswith(old['content']):
                    data['content_delta'] = message['content'][len(old['content']):]
                else:
                    data['content'] = message['content']
            self.writer.append('message.updated', data)
            old.clear()
            old.update(copy.deepcopy(message))

        # 普通状态先落盘，压缩提交作为最后一条事件。这样前面的写入失败
        # 不会留下“上下文已压缩、控制器却回滚”的相反事实。
        for key, kind in (('state', 'state.changed'), ('rules', 'permissions.changed'), ('model_ref', 'model.changed')):
            if key == 'state':
                before = {name: value for name, value in previous[key].items()
                          if name not in {'request_usage', 'compaction_activities'}}
                after = {name: value for name, value in snapshot[key].items()
                         if name not in {'request_usage', 'compaction_activities'}}
                if completed_activity is not None:
                    after['context_management'] = before['context_management']
            else:
                before, after = previous[key], snapshot[key]
            if before == after:
                continue
            data = after if key == 'state' else {key: snapshot[key]}
            self.writer.append(kind, data)
            if key == 'state':
                preserved = {name: previous[key][name] for name in ('request_usage', 'compaction_activities')}
                previous[key] = copy.deepcopy(after)
                previous[key].update(preserved)
            else:
                previous[key] = copy.deepcopy(snapshot[key])

        before, after = previous['transcript'], snapshot['transcript']
        if len(after) < len(before) or context_event == 'context.compacted':
            data = {'messages': after}
            if completed_activity is not None:
                data.update(activity_id=context_activity_id, compaction=completed_activity,
                            context_management=snapshot['state']['context_management'])
            self.writer.append(context_event, data)
            previous['transcript'] = copy.deepcopy(after)
            if completed_activity is not None:
                previous['state']['compaction_activities'][context_activity_id] = copy.deepcopy(completed_activity)
                previous['state']['context_management'] = copy.deepcopy(snapshot['state']['context_management'])
        else:
            for index, old in enumerate(before):
                if old != after[index]:
                    self.writer.append('transcript.updated', {'index': index, 'message': after[index]})
                    before[index] = copy.deepcopy(after[index])
            if len(after) > len(before):
                self.writer.append('transcript.appended', {'messages': after[len(before):]})
                before.extend(copy.deepcopy(after[len(before):]))

    def compaction_committed(self, activity_id: str) -> bool:
        saved = self._saved
        return bool(saved is not None and saved["state"]["compaction_activities"].get(activity_id, {}).get("status") == "completed")

    def record(self, kind, data=None, *, turn_id=None):
        if self.writer is None:
            raise SessionRepositoryError('尚未创建会话。')
        self.writer.append(kind, data or {}, turn_id=turn_id)

    def record_execution(self, kind, data, *, turn_id=None):
        """保持日志与 checkpoint 投影同步，供后台运行时绑定的唯一写入者使用。"""
        self.record(kind, data, turn_id=turn_id)
        SessionCodec.apply_execution_event(self._saved['state']['execution'], kind, data)

    def record_usage(self, data, *, turn_id=None):
        """用量帧单独追加，checkpoint 与事件重放得到同一请求快照。"""
        if self.writer is None:
            return
        if self._saved['state']['request_usage'].get(data['request_id']) == data:
            return
        self.record('usage.request_updated', data, turn_id=turn_id)
        self._saved['state']['request_usage'][data['request_id']] = copy.deepcopy(data)

    def record_compaction(self, data):
        """压缩活动只追加变化的单条快照，避免每次状态变化重写整个活动表。"""
        if self.writer is None:
            return
        saved = self._saved['state']['compaction_activities']
        if saved.get(data['id']) == data:
            return
        self.record('compaction.updated', data, turn_id=data.get('turn_id'))
        saved[data['id']] = copy.deepcopy(data)

    def rename(self, session_id, title):
        if self.paths is not None and self.paths.session_id == session_id and self.writer is not None:
            title = title.strip()
            if not title:
                raise SessionRepositoryError('会话标题不能为空。')
            self.writer.append('session.renamed', {'title': title})
            self.title = title
        else:
            self.repository.rename(session_id, title)

    def checkpoint(self):
        if self.writer is not None:
            self.writer.checkpoint(self._saved)

    def close(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None
