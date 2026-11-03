"""无第三方依赖的样本、判分、统计层；离线验证不会访问模型。"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def tasks():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    raw = (ROOT / 'data/tasks.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['tasks_sha256']:
        raise ValueError('样本校验失败')
    rows = [json.loads(line) for line in raw.splitlines()]
    if [r['task_id'] for r in rows] != manifest['task_ids'] or len(rows) != 16:
        raise ValueError('样本数量或顺序错误')
    return rows

def prompt(task):
    return ('Implement the function in solution.py according to its docstring. '
            'Use only the Python standard library. You may inspect files, edit and run your own tests. '
            'Do not change the required function name or signature. Finish with the complete implementation saved in solution.py. '
            'The evaluator will run separate tests after you finish.\n\n' + task['prompt'])

def prepare_workspace(task, path):
    path.mkdir(parents=True, exist_ok=False)
    (path / 'solution.py').write_text(task['prompt'] + '    pass\n')
    (path / 'TASK.md').write_text(prompt(task))
    # 不向模型提供评分测试、标准答案或其他样本。
    return path

def execute(argv, *, cwd, timeout, stdin=None):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
           'PYTHONHASHSEED': '0', 'PYTHONIOENCODING': 'utf-8'}
    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=out, stderr=err, start_new_session=True)
        timed_out = False
        try:
            p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        finally:
            # 清理程序已退出但遗留的同组子进程。
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out.seek(0); err.seek(0)
        return dict(returncode=p.returncode, timeout=timed_out,
                    elapsed_seconds=time.monotonic()-started,
                    stdout=out.read(65536).decode(errors='replace'),
                    stderr=err.read(65536).decode(errors='replace'))

def grade(task, code, *, trusted_offline=False, image=None, timeout=10):
    # 原始 check() 是一个完整测试套件；不把 AST 中 assert 数量伪装成运行用例数。
    import secrets
    marker = secrets.token_hex(24)
    program = code + '\n' + task['test'] + '\ncheck(' + task['entry_point'] + ')\n' + 'print(' + repr(marker) + ')\n'
    with tempfile.TemporaryDirectory(prefix='he16-grade-') as folder:
        if trusted_offline:
            runner = ('import resource\n'
                      'resource.setrlimit(resource.RLIMIT_CPU,(8,8))\n'
                      'resource.setrlimit(resource.RLIMIT_AS,(536870912,536870912))\n'
                      'resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576))\n'
                      'import sys\nexec(compile(sys.stdin.read(),"candidate.py","exec"),{})\n')
            result = execute([sys.executable, '-I', '-c', runner], cwd=folder,
                             timeout=timeout, stdin=program.encode())
        else:
            if not image or '@sha256:' not in image:
                raise ValueError('模型产出的代码必须在固定 digest 的 Docker 评分镜像中运行')
            import uuid
            name = 'he16-' + uuid.uuid4().hex
            cmd = ['docker', 'run', '--rm', '--name', name, '--network=none', '--read-only',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64',
                   '--memory=512m', '--cpus=1', '--user=65534:65534', '--tmpfs', '/tmp:rw,size=32m',
                   '-i', image, 'python', '-I', '-']
            try:
                result = execute(cmd, cwd=folder, timeout=timeout, stdin=program.encode())
            finally:
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    err = result.pop('stderr')
    finished = marker in result.pop('stdout').splitlines()  # 不持久化候选程序输出，避免泄漏和伪造结果。
    failure = ('test_timeout' if result['timeout'] else
               None if result['returncode'] == 0 and finished else
               'premature_exit' if result['returncode'] == 0 else
               'assertion_failure' if 'AssertionError' in err else
               'syntax_error' if ('SyntaxError' in err or 'IndentationError' in err) else 'runtime_error')
    return dict(result, passed=failure is None, failure_type=failure,
                test_suites_total=1, test_suites_passed=int(failure is None),
                individual_test_count=None)

def cost(usage, rates):
    # 缓存包含在输入中，推理包含在输出中，不重复加算；缺项保持未知。
    names = ('input_tokens','output_tokens','cached_input_tokens')
    if any(usage.get(k) is None for k in names) or not usage.get('complete', False):
        return None
    i, o, c = (usage[k] for k in names)
    if min(i,o,c) < 0 or c > i:
        raise ValueError('非法 token 账本')
    return ((i-c)*rates['input'] + c*rates['cached'] + o*rates['output']) / 1e6

def known_sum(rows,key):
    values=[r.get('usage',{}).get(key) for r in rows]
    return sum(values) if all(v is not None for v in values) else None

def summarize(rows):
    grouped = {}
    expected = {t['task_id'] for t in tasks()}
    for row in rows:
        key = (row['evidence_kind'], row['agent'])
        grouped.setdefault(key, []).append(row)
    summary = []
    for (kind, agent), group in sorted(grouped.items()):
        ids = [r['task_id'] for r in group]
        if len(ids) != len(set(ids)) or not set(ids) <= expected:
            raise ValueError('重复或未知任务，禁止混入同一轮实验')
        completed = [r for r in group if r.get('test') is not None]
        successes = sum(r['test']['passed'] for r in completed)
        n = len(group)
        p = successes/n
        z = 1.96
        center = (p+z*z/(2*n))/(1+z*z/n)
        half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
        costs = [r.get('cost_usd') for r in group]
        summary.append(dict(evidence_kind=kind, agent=agent, planned=16, recorded=n,
            missing=sorted(expected-set(ids)), graded=len(completed), succeeded=successes,
            success_rate=successes/16 if kind == 'measured_online' and n == 16 else None,
            offline_check_passed=successes if kind != 'measured_online' else None,
            observed_rate=successes/n, wilson95=[max(0,center-half),min(1,center+half)] if kind=='measured_online' else None,
            elapsed_seconds=sum(r.get('elapsed_seconds',0) for r in group),
            tool_calls=sum(r.get('tool_calls',0) or 0 for r in group),
            input_tokens=known_sum(group,'input_tokens'),
            output_tokens=known_sum(group,'output_tokens'),
            cost_usd=sum(costs) if all(c is not None for c in costs) else None,
            failure_types=dict(Counter(r.get('failure_type') for r in group if r.get('failure_type')))))
    return summary

def offline(output):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in tasks():
        for label, code in [('reference', task['prompt']+task['canonical_solution']),
                            ('empty', task['prompt']+'    raise NotImplementedError\n')]:
            result = grade(task, code, trusted_offline=True)
            rows.append(dict(task_id=task['task_id'], agent=label, evidence_kind='offline_grader_control',
                test=result, elapsed_seconds=result['elapsed_seconds'], tool_calls=0,
                usage={'input_tokens':None,'output_tokens':None,'cached_input_tokens':None,'complete':False},
                cost_usd=0.0, failure_type=result['failure_type']))
    (output/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (output/'summary.json').write_text(json.dumps(summarize(rows),indent=2)+'\n')
    if not all(r['test']['passed'] == (r['agent']=='reference') for r in rows):
        raise AssertionError('正负对照失败')
    print('Offline grader controls: 16 reference pass; 16 empty fail. No model calls.')

def plan(model):
    rates = {'gpt-6-luna':dict(input=.10,cached=.01,output=.50),
             'gpt-6.1-terra':dict(input=1,cached=.05,output=5)}[model]
    scenarios = []
    for label, turns, inp, out in [('planning',10,8000,1000),('heavy',50,20000,2000),('large_context',50,128000,8192)]:
        requests=32*turns
        amount=requests*(inp*rates['input']+out*rates['output'])/1e6
        scenarios.append(dict(label=label,requests=requests,input_per_request=inp,
                              output_per_request=out,uncached_cost_usd=round(amount,4)))
    return dict(status='not_approved_not_run',model=model,agents=2,tasks=16,runs=32,max_rounds=50,
        max_main_rounds=1600,extra_requests='Retries, compaction and output continuation must be metered separately',
        rates_per_million=rates,price_date='2026-11-03',
        price_source='https://developers.openai.com/api/docs/models/'+model,
        assumptions='Standard tier, input <=272K, no cache writes/discounts; estimates are not caps',
        proposed_global_budget_usd=25,reserve_usd=5,scenarios=scenarios,
        blockers=['User approval before any billed batch','Working OS sandbox with no grader data visible',
                  'Responses API credential/model probe', 'Gateway hard budget enforcement including retries',
                  'Pinned sandbox image and identical task Python runtime'])

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('offline');p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('plan');p.add_argument('--model',choices=['gpt-6-luna','gpt-6.1-terra'],default='gpt-6-luna')
    p=sub.add_parser('summarize');p.add_argument('file',type=Path)
    args=parser.parse_args()
    if args.command=='offline':offline(args.output)
    elif args.command=='plan':print(json.dumps(plan(args.model),indent=2))
    else:print(json.dumps(summarize([json.loads(l) for l in args.file.read_text().splitlines()]),indent=2))
