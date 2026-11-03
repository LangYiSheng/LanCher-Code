"""标准化两套 Agent 的实际输出；不把缺失用量当成零。"""
from __future__ import annotations


def lancher_metrics(events):
    finals=[e['result'] for e in events if e.get('kind')=='result']
    if len(finals)!=1:
        raise ValueError('必须有且只有一个最终 AgentResult')
    result=finals[0];raw=result.get('run_usage') or {};n=raw.get('request_count')
    usage={}
    for field,count in [('input_tokens','input_reported_request_count'),
                        ('output_tokens','output_reported_request_count'),
                        ('cached_input_tokens','cache_reported_request_count')]:
        usage[field]=raw.get(field) if n is not None and raw.get(count)==n else None
    usage['complete']=n is not None and raw.get('incomplete_request_count')==0 and raw.get('invalid_request_count',0)==0
    status=result.get('status')
    failure=None if status=='completed' else 'agent_error'
    if status=='cancelled':failure='cancelled'
    if '上限' in (result.get('error_text') or ''):failure='round_limit'
    return dict(agent_status=status,model_requests=n,
        tool_calls=sum(e.get('kind')=='tool_call_started' for e in events),
        tool_errors=sum(e.get('kind')=='tool_result_received' and bool((e.get('tool_result') or {}).get('is_error')) for e in events),
        usage=usage,failure_type=failure)


def mini_metrics(trajectory):
    info=trajectory.get('info',{});stats=info.get('model_stats',{})
    responses=[m for m in trajectory.get('messages',[]) if m.get('object')=='response']
    usage={'input_tokens':0,'output_tokens':0,'cached_input_tokens':0,'complete':True}
    for message in responses:
        raw=message.get('usage') or {}
        values={'input_tokens':raw.get('input_tokens'),'output_tokens':raw.get('output_tokens'),
                'cached_input_tokens':(raw.get('input_tokens_details') or {}).get('cached_tokens')}
        for key,value in values.items():
            if value is None:usage[key]=None;usage['complete']=False
            elif usage[key] is not None:usage[key]+=value
    # 原生轨迹不能证明重试尝试的用量完整；线上成本以网关逐请求账本为准。
    usage['complete']=False
    if not responses:
        usage={k:None for k in ('input_tokens','output_tokens','cached_input_tokens')}|{'complete':False}
    status=info.get('exit_status')
    failures={'Submitted':None,'LimitsExceeded':'round_or_cost_limit','TimeExceeded':'agent_timeout',
              'RepeatedFormatError':'format_error'}
    return dict(agent_status=status,model_requests=stats.get('api_calls'),
        tool_calls=sum(len(m.get('extra',{}).get('actions',[])) for m in trajectory.get('messages',[])),
        usage=usage,failure_type=failures.get(status,'agent_error'))


def paired_outcomes(rows):
    # 只比较同一轮在线实验；缺失一方保留缺失，不自动算另一方赢。
    pairs={}
    for r in rows:
        if r['evidence_kind']!='measured_online':continue
        pair=pairs.setdefault(r['task_id'],{})
        if r['agent'] in pair:raise ValueError('重复结果')
        pair[r['agent']]=r
    result=dict(both_pass=0,lancher_only=0,mini_only=0,both_fail=0,incomplete=0)
    for pair in pairs.values():
        if set(pair)!={'lancher','mini-swe-agent'} or any(r.get('test') is None for r in pair.values()):
            result['incomplete']+=1;continue
        a,b=(pair[k]['test']['passed'] for k in ('lancher','mini-swe-agent'))
        result['both_pass' if a and b else 'lancher_only' if a else 'mini_only' if b else 'both_fail']+=1
    return result
