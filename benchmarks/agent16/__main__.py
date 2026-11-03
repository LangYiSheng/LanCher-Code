"""python -m benchmarks.agent16 {offline,plan,summary,online}。在线未获准前封闭。"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from .core import ROOT, tasks, summarize, grade_trusted_fixture
from .adapters import lancher_fixture, mini_fixture

def plan():
    return dict(mode='not_run', tasks=16, agents=['lancher', 'mini-swe-agent==2.4.6'],
        max_turns=50, planned_runs=32, max_primary_model_requests=1600,
        retry_and_compaction_requests='must be separately counted and budgeted before online enablement',
        model='gpt-6-luna', api='Responses', reasoning_effort='medium',
        official_api_key_verified=False, api_cost_incurred_usd=0,
        pricing_source='https://developers.openai.com/api/docs/models/gpt-6-luna',
        pricing_checked_on='2026-11-03', rates_per_million=dict(input=0.10, cached_input=0.01, output=0.50),
        scenarios=[dict(requests=calls, mean_input_tokens=i, mean_output_tokens=o,
                        estimated_usd=calls*(i*0.10+o*0.50)/1e6)
                   for calls, i, o in [(320,8000,1500),(1600,20000,2000),(1600,100000,4000)]],
        budget_usd=30, approval=False,
        blockers=['online adapter and hard request/budget guard not yet enabled',
                  'OS sandbox unavailable here: bwrap namespace creation prohibited',
                  'confirm model access and both Responses adapters with approved pilot'],
        note='Estimates only; cumulative context, reasoning, retries and cache billing affect actual spend.')

def offline(out):
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in tasks():
        for name in ['lancher', 'mini-swe-agent']:
            with tempfile.TemporaryDirectory(prefix='agent16-fixture-') as tmp:
                workspace = Path(tmp)
                (workspace/'solution.py').write_text(task['prompt']+'    pass\n')
                solution = task['prompt']+task['canonical_solution']
                meta = (asyncio.run(lancher_fixture(task, workspace, solution)) if name == 'lancher'
                        else mini_fixture(task, workspace, solution))
                candidate = (workspace/'solution.py').read_text()
                # 仅完全匹配可信参考解才执行，禁止意外变成任意代码执行入口。
                if candidate != solution:
                    raise RuntimeError('固定模型写入内容异常')
                grade = grade_trusted_fixture(task, candidate)
                rows.append(dict(mode='offline_simulation', agent=name, task_id=task['task_id'],
                    **meta, **grade, input_tokens=None, output_tokens=None, cost_usd=0,
                    model='deterministic-fixture', max_turns=50))
    (out/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    pending = [dict(mode='online_measured', agent=a, task_id=t['task_id'], status='not_run',
                    tests_passed=None, failure_type=None) for t in tasks() for a in ['lancher','mini-swe-agent']]
    (out/'summary.json').write_text(json.dumps(summarize(rows+pending), indent=2)+'\n')
    (out/'plan.json').write_text(json.dumps(plan(), indent=2)+'\n')
    print(json.dumps(summarize(rows+pending), indent=2))

def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['offline','plan','summary','online'])
    p.add_argument('--output', type=Path, default=Path('agent16-results'))
    p.add_argument('--input', type=Path)
    args = p.parse_args()
    if args.command == 'offline': offline(args.output)
    elif args.command == 'plan': print(json.dumps(plan(), indent=2))
    elif args.command == 'summary':
        if args.input is None: p.error('--input required')
        print(json.dumps(summarize([json.loads(x) for x in args.input.read_text().splitlines()]), indent=2))
    else:
        p.exit(2, 'ONLINE BLOCKED: approval, isolated execution and budget-enforced adapters are required. No API request sent.\n')

if __name__ == '__main__': main()
