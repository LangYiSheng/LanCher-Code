"""安装版 mini-SWE-agent + 官方确定性模型，验证 bash 编辑/提交链路。"""
import json
import shlex
import sys
from pathlib import Path
import yaml
import minisweagent
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output


def run(payload):
    command='python -c '+shlex.quote('from pathlib import Path; Path("solution.py").write_text('+repr(payload['code'])+')')
    actions=[command]*50 if payload.get('endless') else ['cat solution.py',command,'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT']
    model=DeterministicModel(outputs=[make_output('offline fixture',[{'command':a}],cost=0) for a in actions],cost_per_call=0)
    config=yaml.safe_load((Path(minisweagent.__file__).parent/'config/mini.yaml').read_text())['agent']
    # 保留上游系统/任务模板，固定步数；总预算由后续网关控制，避免默认3美元提前截断。
    config={k:v for k,v in config.items() if k in ('system_template','instance_template')}
    env=LocalEnvironment(cwd=payload['cwd'],timeout=30)
    agent=DefaultAgent(model,env,**config,step_limit=50,cost_limit=0,wall_time_limit_seconds=1800)
    result=agent.run(payload['prompt'])
    return dict(status=result['exit_status'],model_rounds=agent.n_calls,
        tool_calls=sum(len(m.get('extra',{}).get('actions',[])) for m in agent.messages),
        evidence_kind='offline_agent_simulation',usage=None,cost_usd=0.0)

if __name__=='__main__':
    print(json.dumps(run(json.load(sys.stdin))))
