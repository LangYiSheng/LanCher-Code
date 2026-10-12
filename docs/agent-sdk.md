# Agent SDK 与无界面运行

`lancher_code.sdk` 提供独立的编程 Agent。TUI、无界面命令和嵌入应用复用同一个服务，包含模型选择、事件、工具执行、权限、会话、Skills、MCP 和取消收尾。

## 安装

```bash
pip install .          # 核心与无界面命令
pip install ".[tui]"  # 加装终端界面
```

仓库开发时，`uv sync` 默认安装 TUI 依赖组；`uv sync --no-default-groups` 只安装核心。`lancher --help` 和 `lancher run --help` 不导入界面。

## 无界面命令

```bash
lancher run "阅读项目并总结入口职责" --config ./my-model.yaml --cwd ./my-project
lancher run "检查修改后的逻辑" --config ./my-model.yaml --model demo/chat --format json --timeout 120
lancher run "继续调查" --config ./my-model.yaml --cwd ./my-project --session 99afd728580d4245871562151eef5259 --format jsonl
```

命令会调用显式配置中的模型；配置结构见 `lancher.example.yaml`。任务是普通自然语言，TUI 的斜杠命令由界面解释，不作为无界面命令解析。

| 参数 | 说明 |
|---|---|
| `prompt` | 必填任务；`-` 从标准输入读到 EOF，保留多行文本，空任务报错 |
| `--config PATH` | 必填 YAML；相对路径按启动目录解析，不进入首次配置界面 |
| `--cwd PATH` | 工具和会话工作目录，默认启动目录；不改变进程的全局工作目录 |
| `--model REF` | 本次 `供应商ID/模型ID`；恢复会话时优先于保存的模型 |
| `--session UUID` | 恢复工作目录内的完整 32 位 UUID；不重跑旧工具或暂停的旧队列 |
| `--format text\|json\|jsonl` | 默认 `text`，另两种供程序消费 |
| `--timeout SECONDS` | 有限正数；包含能力初始化和任务处理，触及时先停止并完成资源清理 |

PowerShell 可以从文件提供多行任务：

```powershell
Get-Content -Raw .\task.txt | lancher run - --config .\my-model.yaml --format json
```

命令保留配置中的阶段和访问范围，不自动切成 `full`。需要批准的操作默认拒绝，模型收到工具错误后可继续调整；项目内允许的操作仍会执行。配置设为 `full` 时按完全访问语义运行。权限是应用层检测，未增加操作系统沙箱。

`text` 把最终回答写 stdout，错误写 stderr。`json` 在 stdout 输出一个 `AgentResult`。`jsonl` 每行输出事件快照，最后一行是：

```json
{"kind":"result","result":{"status":"completed","session_id":"...","text":"...","usage":{},"error_text":null,"run_usage":{}}}
```

示例省略了用量字段，实际输出带完整结构。stdout 不输出引导或退出告别；失败、缺失配置、超时也按选定机器格式输出结果。错误说明仍写 stderr。无界面标准输出和错误输出统一使用 UTF-8，`-` 的标准输入也按 UTF-8 读取；管道两端应使用 UTF-8，不依赖 Windows 默认代码页或 `PYTHONIOENCODING` 的旧编码设置。

| 退出码 | 含义 |
|---|---|
| `0` | 任务完成 |
| `1` | 任务失败、配置错误或资源关闭失败 |
| `2` | 命令行参数错误 |
| `124` | 达到本次命令总超时 |
| `130` | 取消或 Ctrl+C 中断 |

## Python 调用

`create_agent()` 接受显式 `AgentConfig`，不读取全局模型配置、不启动配置引导。已有配置可以转换成不含 UI 的独立副本：

```python
import asyncio
from pathlib import Path
from lancher_code.config.loader import load_config
from lancher_code.sdk import create_agent


async def main():
    config = load_config(Path("my-model.yaml")).to_agent_config()
    async with create_agent(config, cwd=Path("my-project")) as agent:
        result = await agent.run("阅读入口并总结数据流")
        print(result.status, result.text)
        print(result.to_dict())


asyncio.run(main())
```

`async with` 启动服务并等待能力准备完成，离开时停止任务、关闭托管进程与 MCP、保存会话并释放写入锁。不要遗留未关闭的事件任务。

也可以显式 `await agent.ready()` 等待初始化。仅调用 `start_capabilities()` 会在后台初始化，允许像 TUI 一样立即使用已经注册的内置能力；这种模式的首轮 MCP 工具目录可能仍在更新。

`AgentConfig` 包含 `providers`、`default_model`、`runtime` 和 `execution`。工厂支持注入 `provider_factory`、`tool_registry`、`skills_service`、`mcp_manager`；`enable_mcp=False` 关闭 MCP 装配。

`run()` 收集最终结果，默认拒绝需要批准的请求。界面、增量记录和宿主审批可以消费事件：

```python
from contextlib import aclosing
from lancher_code.permissions.models import PermissionResolution

async with create_agent(config, cwd=workspace) as agent:
    async with aclosing(agent.run_user_turn("检查项目")) as events:
        async for event in events:
            host.publish(event.to_dict())
            if event.kind == "permission_request_created":
                request = event.permission_request
                # 示例明确拒绝；实际宿主应取得用户决定。
                agent.resolve_permission_request(
                    PermissionResolution(request_id=request.request_id, outcome="deny")
                )
    result = agent.last_result
```

事件带 `session_id` 和服务内递增 `sequence`。事件、状态和结果是对外快照，外部修改不会改写内部会话；`to_dict()` 输出 JSON 兼容值。流式调用中的审批必须得到处理，否则核心继续等待；提前结束消费时用 `aclosing` 收拢任务。

服务提供模型切换、会话恢复、阶段与访问、忙时输入、计划确认、能力状态、压缩、进程管理与停止接口。成功完成当前轮次后自动推进未暂停的 `follow_up` 队列，失败或取消会暂停队列。一个服务同一时间处理一个任务；并行独立会话分别创建服务。

## 结果与评测

`AgentResult` 包含 `status`、`session_id`、`text`、`usage`、`error_text`、`run_usage`。`usage` 是当前返回回答的用量，`run_usage` 是服务本次启动的实际请求账本，包含压缩、重试和队列执行消耗。未知上报保持 `null`。

实验脚本可以为每个用例准备独立工作目录，固定模型、阶段、权限、Skills、MCP 和项目约定，运行 Agent 后由用例验证程序计算分数。任务完成状态只是运行结果，不能当作质量分数。本次实现与验证不包含真实模型调用或在线评测得分。

会话仍保存在 `工作目录/.lancher/sessions/<UUID>`。Skills 默认读取项目和用户目录，MCP 默认读取项目和用户配置；可复现评测通过注入 Skills/MCP 服务或禁用 MCP 固定来源。

核心仍面向编程任务，提示词、计划和工具带有该产品语义。Web 传输、多用户隔离、认证和其他领域的提示词策略由宿主另行实现。
