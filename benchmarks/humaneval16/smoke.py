"""两套真实 Agent 的离线集成测试。仅脚本化响应携带标准答案。"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from harness import ROOT, execute, grade, prepare_workspace, prompt, summarize, tasks

def main(lancher_python,mini_python,output):
    output.mkdir(parents=True,exist_ok=False)
    rows=[]
    for agent,python,script in [('lancher',lancher_python,'worker_lancher.py'),('mini-swe-agent',mini_python,'worker_mini.py')]:
        for task in tasks():
            with tempfile.TemporaryDirectory(prefix='he16-agent-') as directory:
                cwd=prepare_workspace(task,Path(directory)/'work')
                payload=dict(cwd=str(cwd),code=task['prompt']+task['canonical_solution'],prompt=prompt(task))
                result=execute([python,str(ROOT/script)],cwd=cwd,timeout=60,stdin=json.dumps(payload).encode())
                if result['returncode'] or result['timeout']:
                    raise RuntimeError(f'{agent}: {result["stderr"][-2000:]}')
                metrics=json.loads(result['stdout'].splitlines()[-1])
                metrics.pop('usage',None)
                test=grade(task,(cwd/'solution.py').read_text(),trusted_offline=True)
                rows.append(dict(task_id=task['task_id'],agent=agent,**metrics,test=test,
                    elapsed_seconds=result['elapsed_seconds'],failure_type=test['failure_type'],
                    usage={'input_tokens':None,'output_tokens':None,'cached_input_tokens':None,'complete':False}))
                assert test['passed'] and metrics['model_rounds']==3 and metrics.get('tool_errors',0)==0, (agent,task['task_id'],metrics,test)
    (output/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (output/'summary.json').write_text(json.dumps(summarize(rows),indent=2)+'\n')
    # 第50轮边界实测，两边均保留50轮正常调用空间。
    boundaries=[]
    for agent,python,script in [('lancher',lancher_python,'worker_lancher.py'),('mini-swe-agent',mini_python,'worker_mini.py')]:
        with tempfile.TemporaryDirectory(prefix='he16-limit-') as directory:
            task=tasks()[0];cwd=prepare_workspace(task,Path(directory)/'work')
            payload=dict(cwd=str(cwd),code=task['prompt']+'    pass\n',prompt=prompt(task),endless=True)
            result=execute([python,str(ROOT/script)],cwd=cwd,timeout=90,stdin=json.dumps(payload).encode())
            if result['returncode']:raise RuntimeError(result['stderr'][-2000:])
            metrics=json.loads(result['stdout'].splitlines()[-1])
            assert metrics['model_rounds']==50 and metrics['tool_calls']==50, metrics
            boundaries.append(dict(agent=agent,**metrics))
    (output/'round_limits.json').write_text(json.dumps(boundaries,indent=2)+'\n')
    print('32 offline scripted agent runs passed; both 50-round boundaries passed; 0 API calls.')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--lancher-python',required=True);p.add_argument('--mini-python',required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    main(str(Path(a.lancher_python).absolute()),str(Path(a.mini_python).absolute()),a.output)
