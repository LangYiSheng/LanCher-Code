from __future__ import annotations

import copy
from uuid import uuid4

from lancher_code.sessions.codec import SessionCodec
from lancher_code.sessions.repository import ProjectSessionRepository, SessionRepositoryError


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

    def persist(self, snapshot, *, context_event='context.replaced'):
        if self.writer is None:
            return
        previous = self._saved
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

        before, after = previous['transcript'], snapshot['transcript']
        if len(after) < len(before) or context_event == 'context.compacted':
            self.writer.append(context_event, {'messages': after})
            previous['transcript'] = copy.deepcopy(after)
        else:
            for index, old in enumerate(before):
                if old != after[index]:
                    self.writer.append('transcript.updated', {'index': index, 'message': after[index]})
                    before[index] = copy.deepcopy(after[index])
            if len(after) > len(before):
                self.writer.append('transcript.appended', {'messages': after[len(before):]})
                before.extend(copy.deepcopy(after[len(before):]))

        for key, kind in (('state', 'state.changed'), ('rules', 'permissions.changed'), ('model_ref', 'model.changed')):
            if previous[key] == snapshot[key]:
                continue
            data = snapshot[key] if key == 'state' else {key: snapshot[key]}
            self.writer.append(kind, data)
            previous[key] = copy.deepcopy(snapshot[key])

    def record(self, kind, data=None, *, turn_id=None):
        if self.writer is None:
            raise SessionRepositoryError('尚未创建会话。')
        self.writer.append(kind, data or {}, turn_id=turn_id)

    def record_execution(self, kind, data, *, turn_id=None):
        """保持日志与 checkpoint 投影同步，供后台运行时绑定的唯一写入者使用。"""
        self.record(kind, data, turn_id=turn_id)
        SessionCodec.apply_execution_event(self._saved['state']['execution'], kind, data)

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
