"""样本校验、独立判题与严格区分模拟/实测的统计。"""
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).parent

def tasks():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    raw = (ROOT / 'data/tasks.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['tasks_sha256']:
        raise ValueError('样本内容校验失败')
    rows = [json.loads(x) for x in raw.decode().splitlines()]
    if len(rows) != 16 or [t['task_id'] for t in rows] != manifest['task_ids'] or len(set(manifest['task_ids'])) != 16:
        raise ValueError('样本 ID、顺序或数量校验失败')
    return rows

def prompt(task):
    return ('Implement the function in solution.py according to its docstring. '
            'You may inspect files, edit code and run your own tests. '
            'Use only the Python standard library. Finish by saving solution.py. '
            'The external evaluation tests are unavailable during the task.\n\n' + task['prompt'])

def grade_trusted_fixture(task, solution, timeout=5):
    """仅执行官方参考解和本仓库固定故障夹具；此函数不是安全沙箱。"""
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='agent16-judge-') as tmp:
        code = ("import resource\nresource.setrlimit(resource.RLIMIT_CPU, (3, 3))\n"
                "resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))\n"
                + solution + '\n' + task['test'] + '\ncheck(' + task['entry_point'] + ')\n')
        path = Path(tmp) / 'judge.py'
        path.write_text(code)
        proc = subprocess.Popen([sys.executable, '-I', str(path)], cwd=tmp,
                                env={'PATH': os.defpath, 'PYTHONHASHSEED': '0'},
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            _, stderr = proc.communicate(timeout=timeout)
            failure = (None if proc.returncode == 0 else
                       'test_failure' if b'AssertionError' in stderr else
                       'syntax_error' if b'SyntaxError' in stderr else 'runtime_error')
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            failure = 'test_timeout'
        return dict(tests_passed=failure is None, failure_type=failure,
                    test_suites_passed=int(failure is None), test_suites_total=1,
                    judge_seconds=time.monotonic() - start)

def summarize(rows):
    seen = set()
    for r in rows:
        key = (r['mode'], r['agent'], r['task_id'])
        if key in seen:
            raise ValueError('重复结果，不允许悄悄覆盖或增加分母')
        seen.add(key)
        if r['task_id'] not in {t['task_id'] for t in tasks()}:
            raise ValueError('未知任务 ID')
    groups = {}
    for mode, agent in sorted({(r['mode'], r['agent']) for r in rows}):
        selected = [r for r in rows if r['mode'] == mode and r['agent'] == agent]
        completed = [r for r in selected if r['status'] != 'not_run']
        passed = sum(r['tests_passed'] is True for r in completed)
        n = len(completed)
        # 未执行的任务不会伪装为失败；报告单独展示覆盖率。
        rate = passed / n if n else None
        interval = None
        if n:
            z = 1.959963984540054
            den = 1 + z*z/n
            mid = (rate + z*z/(2*n))/den
            half = z*math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/den
            interval = [max(0.0, mid-half), min(1.0, mid+half)]
        groups[f'{mode}/{agent}'] = dict(planned=16, executed=n, passed=passed,
            coverage=n/16, success_rate=rate, wilson_95=interval,
            failures=dict(collections.Counter(r['failure_type'] for r in completed if r['failure_type'])),
            metric_totals={field: (sum(r[field] for r in completed)
                if completed and all(r.get(field) is not None for r in completed) else None)
                for field in ['tool_calls', 'model_requests', 'elapsed_seconds', 'input_tokens', 'output_tokens', 'cost_usd']})
    return groups
