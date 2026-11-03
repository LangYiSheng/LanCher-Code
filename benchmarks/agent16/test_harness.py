"""测判题器和统计边界，绝不把参考解结果当成 Agent 成绩。"""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
from .core import tasks, grade_trusted_fixture, summarize
from .adapters import lancher_fixture, mini_fixture


def test_manifest_unique_and_pinned():
    assert len({t['task_id'] for t in tasks()}) == 16

@pytest.mark.parametrize('task', tasks(), ids=lambda t: t['task_id'])
def test_reference_passes_and_stub_fails(task):
    assert grade_trusted_fixture(task, task['prompt']+task['canonical_solution'])['tests_passed']
    assert not grade_trusted_fixture(task, task['prompt']+'    raise NotImplementedError\n')['tests_passed']

def test_judge_syntax_and_timeout():
    t = tasks()[0]
    assert grade_trusted_fixture(t, 'def broken(')['failure_type'] == 'syntax_error'
    assert grade_trusted_fixture(t, 'while True: pass', timeout=.1)['failure_type'] == 'test_timeout'

def test_unknown_usage_and_modes_not_mixed():
    base = dict(agent='a', task_id=tasks()[0]['task_id'], failure_type=None,
                tool_calls=1, model_requests=2, elapsed_seconds=.1, input_tokens=None, output_tokens=None, cost_usd=0)
    rows = [dict(base, mode='offline_simulation', status='completed', tests_passed=True),
            dict(base, mode='online_measured', status='not_run', tests_passed=None)]
    report = summarize(rows)
    assert report['offline_simulation/a']['metric_totals']['input_tokens'] is None
    assert report['online_measured/a']['success_rate'] is None
    assert report['online_measured/a']['executed'] == 0
    with pytest.raises(ValueError): summarize(rows+rows[:1])

@pytest.mark.parametrize('agent', ['lancher','mini'])
def test_real_agent_loop_with_fixture(agent, tmp_path):
    t = tasks()[0]
    solution = t['prompt']+t['canonical_solution']
    (tmp_path/'solution.py').write_text(t['prompt'])
    r = (asyncio.run(lancher_fixture(t,tmp_path,solution)) if agent=='lancher' else mini_fixture(t,tmp_path,solution))
    assert r['model_requests'] == 3
    assert (tmp_path/'solution.py').read_text() == solution
    assert r['tool_calls'] >= 1

def test_online_fails_closed():
    p = subprocess.run([sys.executable,'-m','benchmarks.agent16','online'], capture_output=True, text=True)
    assert p.returncode == 2 and 'No API request sent' in p.stderr

@pytest.mark.parametrize('agent', ['lancher', 'mini'])
def test_exact_50_round_limit(agent, tmp_path):
    t = tasks()[0]
    (tmp_path/'solution.py').write_text(t['prompt'])
    result = (asyncio.run(lancher_fixture(t,tmp_path,'',repeat=True)) if agent=='lancher'
              else mini_fixture(t,tmp_path,'',repeat=True))
    assert result['model_requests'] == 50
    assert result['status'] in {'failed', 'LimitsExceeded'}
