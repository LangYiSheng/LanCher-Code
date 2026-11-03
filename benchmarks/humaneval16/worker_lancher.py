"""真实 SDK + 脚本化模型的离线接入检查；不代表模型解题能力。"""
import asyncio
import json
import sys
from contextlib import aclosing
from pathlib import Path

from lancher_code.agent.skills import SkillsService
from lancher_code.contracts.messages import ContentBlock, StreamEvent
from lancher_code.contracts.tools import ToolCallChunk
from lancher_code.permissions.storage import PermissionStorage
from lancher_code.providers.models import ModelDefinition, ProviderDefinition
from lancher_code.sdk import AgentConfig, PermissionResolution, RuntimeConfig, create_agent

class ScriptedProvider:
    def __init__(self, code, endless=False):
        self.code, self.endless, self.calls = code, endless, 0

    async def stream_chat(self, request):
        self.calls += 1
        if self.calls <= 2 or self.endless:
            name = 'read_file' if self.calls % 2 else 'write_file'
            args = {'path':'solution.py'} if name == 'read_file' else {'path':'solution.py','content':self.code}
            call_id = 'offline-'+str(self.calls)
            yield StreamEvent(kind='tool_call_delta',tool_call_chunk=ToolCallChunk(
                call_index=0,provider_call_id=call_id,name_delta=name,arguments_delta=json.dumps(args)))
            yield StreamEvent(kind='message_end', response_complete=True,
                assistant_blocks=[ContentBlock.tool_use_block(call_id=call_id,name=name,input=args)])
        else:
            yield StreamEvent(kind='text_delta',text='Offline scripted completion')
            yield StreamEvent(kind='message_end',response_complete=True,
                assistant_blocks=[ContentBlock.text_block('Offline scripted completion')])

async def run(payload):
    cwd=Path(payload['cwd']).resolve()
    model=ScriptedProvider(payload['code'],payload.get('endless',False))
    config=AgentConfig(providers={'offline':ProviderDefinition(name='offline',protocol='openai',
        base_url='http://127.0.0.1:1/v1',api_key='offline-placeholder',
        models={'fixture':ModelDefinition(model_name='offline-fixture')})},default_model='offline/fixture',
        runtime=RuntimeConfig(tool_loop_limit=50,work_phase='execute',execution_access='full'))
    counts={'tool_calls':0,'tool_errors':0}
    async with create_agent(config,cwd=cwd,provider_factory=lambda _:model,
        permission_storage=PermissionStorage(),enable_mcp=False,
        skills_service=SkillsService(cwd,user_root=cwd/'empty-home')) as agent:
        async with aclosing(agent.run_user_turn(payload['prompt'])) as events:
            async for event in events:
                if event.kind=='tool_call_started': counts['tool_calls']+=1
                if event.kind=='tool_result_received' and event.tool_result.is_error: counts['tool_errors']+=1
                if event.kind=='permission_request_created':
                    agent.resolve_permission_request(PermissionResolution(event.permission_request.request_id,'deny'))
        result=agent.last_result.to_dict()
    return dict(counts,status=result['status'],model_rounds=model.calls,run_usage=result['run_usage'],
                evidence_kind='offline_agent_simulation',usage=None,cost_usd=0.0)

if __name__=='__main__':
    print(json.dumps(asyncio.run(run(json.load(sys.stdin)))))
