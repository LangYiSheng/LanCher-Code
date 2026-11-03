"""真实 Agent 循环 + 确定性模型：只用于零费用集成验证。"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import time

from .core import prompt

async def lancher_fixture(task, workspace, solution, repeat=False):
    from lancher_code.sdk import create_agent
    from lancher_code.agent.skills import SkillsService
    from lancher_code.config.models import AgentConfig, RuntimeConfig
    from lancher_code.providers.models import ProviderDefinition, ModelDefinition
    from lancher_code.contracts.messages import StreamEvent, ContentBlock
    from lancher_code.contracts.tools import ToolCallChunk
    from lancher_code.permissions.storage import PermissionStorage

    class FixtureProvider:
        calls = 0
        async def stream_chat(self, request):
            self.calls += 1
            if self.calls <= 2 or repeat:
                name = 'read_file' if self.calls == 1 or repeat else 'write_file'
                args = dict(path='solution.py') if self.calls == 1 or repeat else dict(path='solution.py', content=solution)
                yield StreamEvent(kind='tool_call_delta', tool_call_chunk=ToolCallChunk(
                    call_index=0, provider_call_id=f'fixture-{self.calls}', name_delta=name,
                    arguments_delta=json.dumps(args)))
                yield StreamEvent(kind='message_end', response_complete=True, assistant_blocks=[
                    ContentBlock.tool_use_block(call_id=f'fixture-{self.calls}', name=name, input=args)])
            else:
                yield StreamEvent(kind='text_delta', text='Fixture complete')
                yield StreamEvent(kind='message_end', response_complete=True,
                    assistant_blocks=[ContentBlock.text_block('Fixture complete')])

    provider = FixtureProvider()
    config = AgentConfig(providers={'fixture': ProviderDefinition(name='Offline fixture', protocol='openai',
        base_url='http://invalid.invalid', api_key='offline-placeholder-not-a-credential', models={'fixture': ModelDefinition(model_name='fixture')})},
        default_model='fixture/fixture', runtime=RuntimeConfig(tool_loop_limit=50, execution_access='full'))
    start = time.monotonic()
    async with create_agent(config, cwd=workspace, provider_factory=lambda _: provider,
            enable_mcp=False, permission_storage=PermissionStorage(),
            skills_service=SkillsService(workspace, user_root=workspace / 'empty-home')) as agent:
        result = await asyncio.wait_for(agent.run(prompt(task)), timeout=60)
        tool_calls = sum(entry.kind == 'tool_result' for message in agent.messages for entry in message.trace.entries)
        return dict(status=result.status, tool_calls=tool_calls, model_requests=provider.calls,
                    elapsed_seconds=time.monotonic()-start)

def mini_fixture(task, workspace, solution, repeat=False):
    # 禁止加载宿主的全局 .env；离线模型永远不会访问模型网络。
    os.environ['MSWEA_GLOBAL_CONFIG_DIR'] = str(workspace / 'empty-mini-config')
    os.environ['MSWEA_SILENT_STARTUP'] = '1'
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models.test_models import DeterministicModel, make_output
    command = "printf %s " + shlex.quote(solution) + ' > solution.py'
    model = DeterministicModel(cost_per_call=0, outputs=[
        make_output('Read fixture', [{'command': 'cat solution.py'}], cost=0),
        make_output('Write fixture', [{'command': command}], cost=0),
        make_output('Finish fixture', [{'command': 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'}], cost=0)])
    if repeat:
        model.config.outputs = [make_output('Read', [{'command': 'cat solution.py'}], cost=0) for _ in range(51)]
    # 此环境只执行本文件构造的可信固定命令，不对外暴露在线执行。
    env = LocalEnvironment(cwd=str(workspace), timeout=10)
    agent = DefaultAgent(model, env, system_template='Offline harness fixture',
                         instance_template='{{ task }}', step_limit=50, cost_limit=0)
    start = time.monotonic()
    result = agent.run(prompt(task))
    return dict(status=result['exit_status'], tool_calls=sum(len(m.get('extra', {}).get('actions', [])) for m in agent.messages),
                model_requests=agent.n_calls, elapsed_seconds=time.monotonic()-start)
