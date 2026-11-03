"""评分边界与统计口径的必要回归，不调用任何真实模型。"""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from harness import cost, grade, plan, prepare_workspace, summarize, tasks
from metrics import lancher_metrics, mini_metrics, paired_outcomes


def test_samples_and_no_answer_leak(tmp_path):
    selected=tasks()
    assert len(selected)==16
    for i,t in enumerate(selected):
        work=prepare_workspace(t,tmp_path/str(i))
        assert set(p.name for p in work.iterdir())=={'TASK.md','solution.py'}
        assert (work/'solution.py').read_text()==t['prompt']+'    pass\n'
        assert t['test'] not in (work/'TASK.md').read_text()

@pytest.mark.parametrize('code,failure',[
    ('def f(): return 2','assertion_failure'),
    ('def f(:','syntax_error'),
    ('raise RuntimeError()','runtime_error'),
    ('import sys; sys.exit(0)','premature_exit'),
    ('while True: pass','test_timeout'),
    ('def f(): return 1',None)])
def test_grading_failures(code,failure):
    t=dict(test='def check(candidate):\n    assert candidate()==1',entry_point='f')
    result=grade(t,code,trusted_offline=True,timeout=.3 if 'while' in code else 5)
    assert result['failure_type']==failure
    assert result['passed']==(failure is None)


def test_untrusted_requires_sandbox():
    with pytest.raises(ValueError):grade(tasks()[0],'pass')


def test_cost_unknown_and_cache():
    rates={'input':1,'cached':.05,'output':5}
    assert cost({'input_tokens':1000,'cached_input_tokens':200,'output_tokens':100,'complete':True},rates)==.00131
    assert cost({'input_tokens':1000,'output_tokens':100,'complete':True},rates) is None
    assert cost({'input_tokens':1000,'cached_input_tokens':0,'output_tokens':100,'complete':False},rates) is None


def test_lancher_uses_run_ledger_not_final_answer():
    events=[{'kind':'tool_call_started'},{'kind':'result','result':{'status':'completed','usage':{'input_tokens':999},
        'run_usage':{'request_count':2,'input_tokens':50,'output_tokens':10,'cached_input_tokens':0,
        'input_reported_request_count':2,'output_reported_request_count':2,'cache_reported_request_count':2,
        'incomplete_request_count':0}}}]
    result=lancher_metrics(events)
    assert result['usage']['input_tokens']==50 and result['tool_calls']==1
    events[-1]['result']['run_usage']['input_reported_request_count']=1
    assert lancher_metrics(events)['usage']['input_tokens'] is None


def test_missing_mini_usage_unknown():
    result=mini_metrics({'info':{'exit_status':'Submitted','model_stats':{'api_calls':2}},'messages':[]})
    assert result['usage']['input_tokens'] is None and not result['usage']['complete']


def test_simulation_never_becomes_success_rate():
    row=dict(task_id=tasks()[0]['task_id'],agent='lancher',evidence_kind='offline_agent_simulation',
             test={'passed':True},usage={'input_tokens':None},cost_usd=0)
    summary=summarize([row])[0]
    assert summary['success_rate'] is None and summary['input_tokens'] is None
    assert len(summary['missing'])==15
    with pytest.raises(ValueError):summarize([row,row])
    assert paired_outcomes([row])['both_pass']==0


def test_plan_and_paired():
    assert plan('gpt-6-luna')['max_main_rounds']==1600
    assert plan('gpt-6-luna')['scenarios'][1]['uncached_cost_usd']==4.8
    rows=[dict(task_id=tasks()[0]['task_id'],agent=a,evidence_kind='measured_online',test={'passed':v})
          for a,v in [('lancher',True),('mini-swe-agent',False)]]
    assert paired_outcomes(rows)['lancher_only']==1
