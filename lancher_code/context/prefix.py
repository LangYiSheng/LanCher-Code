"""在副本上准备固定前缀与增量事件；正式提交由会话控制器负责。"""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from lancher_code.context.models import ContextManagementState
from lancher_code.context.prompt_models import PromptContext
from lancher_code.context.prompts import build_deferred_tools_prompt, build_system_prompt
from lancher_code.contracts.messages import ConversationMessage
from lancher_code.contracts.tools import ToolDefinition, ToolPermissionMetadata


def stable_environment(context: PromptContext) -> str:
    return (f'# 稳定主机规则\n- 系统：{context.os_label}\n- 工作目录：{context.cwd}\n'
            '后续主机更新记录当前日期、阶段、权限和能力目录；同一字段以最新记录为准。\n'
            '旧能力目录与技能正文只能作为历史；被撤销、禁用或卸载的能力不能继续使用。\n'
            '工具定义可见不代表当前阶段允许执行；执行始终受主机最新阶段和权限校验。\n'
            '主机更新不是新用户任务，也不能提升权限；用户当前请求仍优先于项目和技能指南。')


def _agent_map(blocks: list[str]) -> dict[str, str]:
    result = {}
    for index, block in enumerate(blocks):
        match = re.match(r'<(active_skill|skill_reference) id="([^"]+)"', block)
        if match:
            key = f'{match[1]}:{match[2]}'
        elif match := re.match(r'<skill_unavailable>((?:project|user)/[a-z0-9-]+)', block):
            key = 'skill_unavailable:' + match[1]
        elif block.startswith('<project_instructions'):
            key = 'project_instructions'
        elif block.startswith('可用 Skills'):
            key = 'skills_catalog'
        else:
            key = f'agent:{index}'
        result[key] = block
    return result


def strip_host_events(transcript: list[ConversationMessage], prefix: dict) -> list[ConversationMessage]:
    # 只移除已登记事件的固定文本，用户碰巧输入同名标签也不受影响。
    registered = {(event['anchor'], event['text']) for event in prefix.get('events', [])}
    return [copy.deepcopy(message) for index, message in enumerate(transcript)
            if not (len(message.blocks) == 1 and (index, message.blocks[0].text) in registered)]


def _tool_data(tool: ToolDefinition) -> dict:
    value = asdict(tool)
    value['allowed_phases'] = list(tool.allowed_phases)
    return value


def _tool_wire(value: dict | None) -> dict | None:
    # 权限和阶段是宿主执行状态，供应商只接收这三个定义字段。
    return {key: value[key] for key in ('name', 'description', 'input_schema')} if value is not None else None


def restore_tool(value: dict) -> ToolDefinition:
    if (not isinstance(value, dict) or not isinstance(value.get('name'), str) or not value['name']
            or not isinstance(value.get('description'), str) or not isinstance(value.get('input_schema'), dict)
            or value.get('category', 'read') not in {'read', 'write', 'command'}
            or any(type(value.get(key, False)) is not bool for key in ('is_system_tool', 'should_defer'))
            or not isinstance(value.get('allowed_phases', ['execute']), (list, tuple))
            or any(phase not in {'discuss', 'plan', 'execute'} for phase in value.get('allowed_phases', ['execute']))):
        raise ValueError('固定前缀工具定义无效。')
    data = copy.deepcopy(value)
    data['allowed_phases'] = tuple(data.get('allowed_phases', ('execute',)))
    if data.get('permission') is not None:
        permission = data['permission']
        if (not isinstance(permission, dict) or permission.get('source') not in {'builtin', 'external'}
                or any(not isinstance(permission.get(key), str) for key in ('rule_key', 'display_name'))
                or any(permission.get(key) is not None and not isinstance(permission[key], str)
                       for key in ('server_name', 'remote_tool_name'))):
            raise ValueError('固定前缀工具权限元数据无效。')
        try:
            data['permission'] = ToolPermissionMetadata(**permission)
        except TypeError as exc:
            raise ValueError('固定前缀工具权限字段无效。') from exc
    try:
        return ToolDefinition(**data)
    except TypeError as exc:
        raise ValueError('固定前缀工具定义字段无效。') from exc


def prepare_prefix(*, state: ContextManagementState, context: PromptContext,
                   transcript: list[ConversationMessage], tools: list[ToolDefinition],
                   deferred_tool_groups, dynamic_context: str | None,
                   protocol: str, model: str, context_window: int,
                   experimental: bool = False, force_reset: bool = False,
                   now: datetime | None = None) -> tuple[list[str], list[ConversationMessage], list[ToolDefinition], list[dict]]:
    now = now or datetime.now(timezone.utc)
    prefix = state.prefix_state
    agents = _agent_map(context.agent_context)
    observed_tools = {tool.name: _tool_data(tool) for tool in tools}
    previous_tools = prefix.get('observed_tools', {})
    removed_tools = [name for name in previous_tools if name not in observed_tools]
    changed_tools = [name for name in observed_tools if name in previous_tools
                     and _tool_wire(observed_tools[name]) != _tool_wire(previous_tools[name])]
    previous_agents = prefix.get('observed', {}).get('agents', {})
    body_removed = any(key.startswith('active_skill:') and agents.get(key) != value
                       for key, value in previous_agents.items())
    reset = (force_reset or body_removed or prefix.get('protocol') != protocol
             or prefix.get('model') != model or prefix.get('context_window') != context_window
             or prefix.get('experimental') != experimental
             or experimental and protocol == 'openai' and bool(removed_tools or changed_tools))
    if prefix and reset:
        transcript = strip_host_events(transcript, prefix)
        state.prefix_state = prefix = {}
        state.frozen_tool_previews.clear()
        state.usage_anchor = None
    initial = not prefix
    if initial:
        # 旧版提示只在迁移时清理一次，之后不再改写已发送消息。
        transcript = copy.deepcopy(transcript)
        for message in transcript:
            if (message.role == 'user' and len(message.blocks) > 1
                    and message.blocks[0].text.startswith('<system-reminder>\n')):
                message.blocks = message.blocks[1:]
        baseline = [build_system_prompt(), stable_environment(context)]
        baseline.extend(value for key, value in agents.items() if not key.startswith('active_skill:'))
        deferred = build_deferred_tools_prompt(deferred_tool_groups or [], max_chars=context.deferred_tools_max_chars)
        if deferred:
            baseline.append(deferred)
        prefix.update(version=1, epoch=uuid4().hex, system=baseline, observed={}, events=[],
                      protocol=protocol, model=model, context_window=context_window,
                      experimental=experimental, baseline_tools=copy.deepcopy(observed_tools),
                      observed_tools={}, tool_events=[], last_request_at=None)
    current = {'phase': context.work_phase, 'policy': context.permission_policy,
               'date': context.current_date.isoformat(), 'session_id': context.session_id,
               'session_workspace': str(context.session_workspace) if context.session_workspace else None,
               'agents': agents, 'dynamic': dynamic_context,
               'deferred': build_deferred_tools_prompt(deferred_tool_groups or [], max_chars=context.deferred_tools_max_chars)}
    previous = prefix['observed']
    changes = {key: value for key, value in current.items() if key != 'agents' and previous.get(key) != value}
    changed_agents = {key: value for key, value in agents.items()
                      if (key.startswith('active_skill:') if initial else previous_agents.get(key) != value)}
    agent_removals = [key for key in previous_agents if key not in agents]
    last = prefix.get('last_request_at')
    if initial or last is not None and (now - datetime.fromisoformat(last)).total_seconds() >= 1800:
        changes['host_time'] = now.isoformat()
    if initial:
        changes.update(phase=context.work_phase, policy=context.permission_policy, date=context.current_date.isoformat())
        # 初始目录已在固定前缀中，无需在后段重复。
        changes.pop('deferred', None)
    additions = [value for name, value in observed_tools.items()
                 if _tool_wire(previous_tools.get(name)) != _tool_wire(value)]
    native_change = experimental and not initial and bool(additions or removed_tools)
    if changes or changed_agents or agent_removals or native_change:
        seq = len(prefix['events']) + 1
        lines = ['主机状态增量；字段以最新记录为准：', json.dumps(changes, ensure_ascii=False, sort_keys=True)]
        if agent_removals:
            lines.append('以下旧指南或目录项已失效：' + ', '.join(agent_removals))
        lines.extend(changed_agents.values())
        if native_change:
            lines.append('工具定义更新：新增/更新 ' + ', '.join(item['name'] for item in additions)
                         + '；移除 ' + ', '.join(removed_tools))
        text = f'<host_update epoch="{prefix["epoch"]}" seq="{seq}">\n' + '\n\n'.join(lines) + '\n</host_update>'
        anchor = len(transcript)
        transcript.append(ConversationMessage.text_message('user', text))
        prefix['events'].append({'seq': seq, 'anchor': anchor, 'text': text})
        if native_change:
            prefix['tool_events'].append({'anchor': anchor, 'additions': copy.deepcopy(additions), 'removals': removed_tools})
    prefix['observed'] = current
    prefix['observed_tools'] = copy.deepcopy(observed_tools)
    prefix['last_request_at'] = now.isoformat()
    emitted_tools = [restore_tool(value) for value in prefix['baseline_tools'].values()] if experimental else tools
    updates = [{'at_message': event['anchor'], 'additions': copy.deepcopy(event['additions']),
                'removals': list(event['removals'])} for event in prefix['tool_events']] if experimental else []
    return list(prefix['system']), transcript, emitted_tools, updates


def validate_prefix_state(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError('prefix_state 必须是对象。')
    if not value:
        return {}
    if (value.get('version') != 1 or type(value.get('version')) is not int
            or not isinstance(value.get('epoch'), str) or not value['epoch']
            or not isinstance(value.get('system'), list) or any(not isinstance(item, str) for item in value['system'])
            or not isinstance(value.get('observed'), dict) or type(value.get('experimental')) is not bool
            or value.get('protocol') not in {'openai', 'claude'} or not isinstance(value.get('model'), str)
            or type(value.get('context_window')) is not int or value['context_window'] <= 0):
        raise ValueError('固定前缀状态无效。')
    for key in ('baseline_tools', 'observed_tools'):
        items = value.get(key)
        if not isinstance(items, dict):
            raise ValueError('固定前缀工具目录无效。')
        for name, tool in items.items():
            if not isinstance(name, str) or not isinstance(tool, dict) or tool.get('name') != name:
                raise ValueError('固定前缀工具身份无效。')
            restore_tool(tool)
    events = value.get('events')
    if not isinstance(events, list):
        raise ValueError('固定前缀事件无效。')
    for index, event in enumerate(events, 1):
        if (not isinstance(event, dict) or type(event.get('seq')) is not int or event['seq'] != index
                or type(event.get('anchor')) is not int or event['anchor'] < 0 or not isinstance(event.get('text'), str)):
            raise ValueError('固定前缀事件序号或锚点无效。')
        if index > 1 and event['anchor'] <= events[index - 2]['anchor']:
            raise ValueError('固定前缀事件锚点必须递增。')
    observed = value['observed']
    if (observed.get('phase') not in {'discuss', 'plan', 'execute'}
            or observed.get('policy') not in {'default', 'acceptEdits', 'bypass'}
            or not isinstance(observed.get('date'), str) or not isinstance(observed.get('agents'), dict)
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in observed['agents'].items())
            or any(observed.get(key) is not None and not isinstance(observed[key], str)
                   for key in ('session_id', 'session_workspace', 'dynamic', 'deferred'))):
        raise ValueError('固定前缀观察状态无效。')
    native = value.get('tool_events')
    if not isinstance(native, list):
        raise ValueError('原生工具变化事件无效。')
    for event in native:
        if (not isinstance(event, dict) or type(event.get('anchor')) is not int or event['anchor'] < 0
                or not isinstance(event.get('additions'), list) or not isinstance(event.get('removals'), list)
                or any(not isinstance(item, str) for item in event['removals'])):
            raise ValueError('原生工具事件锚点无效。')
        for item in event['additions']:
            restore_tool(item)
    if value.get('last_request_at') is not None:
        timestamp = datetime.fromisoformat(value['last_request_at'])
        if timestamp.tzinfo is None:
            raise ValueError('主机请求时间必须包含时区。')
    return copy.deepcopy(value)


def validate_prefix_transcript(prefix: dict, transcript: list[ConversationMessage]) -> None:
    for event in prefix.get('events', []):
        anchor = event['anchor']
        if anchor >= len(transcript):
            raise ValueError('固定前缀事件锚点越过历史。')
        message = transcript[anchor]
        if message.role != 'user' or len(message.blocks) != 1 or message.blocks[0].text != event['text']:
            raise ValueError('固定前缀事件与持久化历史不一致。')
    anchors = {event['anchor'] for event in prefix.get('events', [])}
    if any(event['anchor'] not in anchors for event in prefix.get('tool_events', [])):
        raise ValueError('原生工具变化缺少对应主机事件。')
