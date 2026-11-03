# Agent16：可复现离线评测底座

本次交付完成 **离线阶段**。32 次确定性模型驱动的真实 Agent 循环与参考解判题已运行；**真实模型任务尚未执行，无在线成功率，API 支出 $0**。`online` 命令故意失败关闭，当前不提供可绕过的付费运行参数。在线适配器、强制费用控制及 OS 隔离仍需实现并验证，不能称为已经完成的在线实验。

## 固定实验

- LanCher-Code 起点：`f984bb72b46d8eff8d82d148782da83a62a3d308`。
- 对照：已实际安装并运行 `mini-swe-agent==2.4.6` 的 `DefaultAgent`。
- 数据：OpenAI HumanEval，commit `b9453e8f1865df0a0057a1383767caa400acfbcf`，保留 MIT 许可。
- 样本：用 `SHA256(seed + ':' + task_id)` 排序取前16，无人工筛选；完整规则、ID、源压缩包及样本 SHA256 见 `manifest.json`。
- 任务：在 `solution.py` 中实现题目函数；公共任务提示完全相同，各 Agent 保留各自原生控制方式。
- 两边上限均50：LanCher `tool_loop_limit=50`，mini `step_limit=50`。模型输出续接、压缩与供应商重试不能从实际请求账本漏掉，在线前须额外加总请求上限守卫。不得把一次工具调用当作一轮。
- 每题、每 Agent 新建临时工作目录；外部判题使用另一个目录。禁用 LanCher MCP、用户 Skills 与持久权限规则；mini 不加载宿主 `.env`。
- 当前模拟只执行仓库控制的固定指令与参考解。目录隔离**不等于安全沙箱**，不能把本地夹具执行函数用来跑模型生成代码。当前宿主 `bwrap` 报 `Creating new namespace failed: Operation not permitted`，也没有 Docker。

HumanEval 是函数级公开数据集，可能有训练污染且规模小。这16题可检验编程任务与工具流程，不能据此声称具备大型仓库修复能力，也不能与完整 HumanEval 榜单直接比较。后续可另立 SWE-bench 实验，不能看过结果后替换这16题。

## 安装与重现

在仓库根目录，Linux + Python 3.14.8：

```bash
uv venv --python 3.14.8 .venv-agent16
uv pip install --python .venv-agent16/bin/python -r benchmarks/agent16/requirements.lock
uv pip install --python .venv-agent16/bin/python --no-deps -e .
.venv-agent16/bin/python -m pytest benchmarks/agent16/test_harness.py -q
.venv-agent16/bin/python -m benchmarks.agent16 offline --output /tmp/agent16-fresh
.venv-agent16/bin/python -m benchmarks.agent16 plan
.venv-agent16/bin/python -m benchmarks.agent16 summary --input /tmp/agent16-fresh/results.jsonl
```

输出目录必须不存在，避免覆盖已有实验。数据已随代码固定，无需联网下载。若重建样本：克隆官方 HumanEval、checkout 上述 commit，再运行 `python -m benchmarks.agent16.prepare /path/to/human-eval`。依赖锁针对本次 Linux/Python 环境，不承诺跨平台二进制完全一致。

## 判定与记录口径

判题拼接候选完整文件、官方 `test` 与 `check(entry_point)`；退出0才通过，超时/语法错误/运行异常均失败。保留原始官方测试套件，不改变断言语义。`test_suites_total=1` 表示一套官方测试，**不声称统计了套件内每条断言或动态测试用例数**。CPU/内存/墙钟限制用于控制可信夹具故障，不构成恶意代码隔离。

每行结果记录 `mode, agent, task_id, model, max_turns, status, tests_passed, failure_type, test_suites_passed, test_suites_total, tool_calls, model_requests, elapsed_seconds, judge_seconds, input_tokens, output_tokens, cost_usd`。

- `status=completed` 只表示 Agent 循环结束，不能代替测试通过。
- `offline_simulation`：真实 Agent 框架 + 预写模型返回 + 公开参考解，Token 为 null，实际 API 费用0；时间是离线宿主耗时，不能比较模型速度。
- `online_measured`：仅真实模型调用才可使用；当前计划32项均 `not_run`，成功率为 null。
- 未上报 Token/成本保持 null，不能填0。汇总缺任一项时总量为 null，防止低估费用。
- 拒绝重复 `(mode, agent, task_id)` 与未知题号。成功率分母为已执行题数，另列计划16题与覆盖率；未跑完不可称“16题成绩”。
- Wilson 95% 区间仅描述这组二元结果的小样本不确定性；模拟区间无模型能力含义。
- mini 完成任务需要一个提交 Shell 动作，因此固定夹具每题工具数3；LanCher 直接结束，每题工具数2。这是交互协议差异，不是效率胜负。

原始32条离线结果、分组汇总、未执行在线计划见 `evidence/offline-20261103/`。单元测试涵盖所有16个参考解通过/空实现失败、语法错误、超时、重复结果、未知用量、模拟与在线分组、两个真实循环及在线阻断。

## 在线实验接入条件（待完成，不能跳过）

1. 为两个 Agent 接入同一模型、Responses API、相同 reasoning effort、输出上限及重试策略。LanCher 已有 Responses 支持，mini 安装包有 Responses 模型类，但本次未验证二者的付费互通。
2. 独立 OS 沙箱/临时 VM；只挂载当前题目目录，可写范围相同。模型代理在沙箱外持有密钥，工具进程不继承密钥；禁用工具联网、宿主 home、其他题目录与评分数据。判题仅在 Agent 停止后运行，评分脚本/参考解不进入 Agent 文件系统。
3. 实现请求前的保守费用预留（包含最大输出、长上下文、缓存写入）和请求后实际用量对账；串行执行，到批准额度前停止，不用缩短50轮来规避费用控制。未知用量暂停，超时请求可能已计费，不能当作0。
4. 从 SDK `run_usage` 读取完整请求账本；从 mini 模型响应读取 input/output/cached/reasoning，用统一价格重算。压缩/重试另列用途。失败类型补齐认证、限流、上下文、工具权限、轮次、费用、模型超时与基础设施错误。
5. 用户批准后先做同题双方各一次接入试跑，再批准或确认正式32次运行。账号可用模型、密钥有效性与余额尚未核验，本次没有使用会话中提供的密钥。

## 待审批预算（估算，不是账单）

建议首先 `gpt-6-luna`，统一 `Responses` + `medium`。2026-11-03 官方 Standard 短上下文价：每百万输入 $0.10、缓存输入 $0.01、输出 $0.50。Terra 输入/输出 $1/$5。来源：

- https://developers.openai.com/api/docs/models/gpt-6-luna
- https://developers.openai.com/api/docs/models/gpt-6.1-terra
- https://developers.openai.com/api/docs/pricing

| 情景 | 主请求数 | 平均输入/输出 Token | Luna | Terra |
| --- | ---: | ---: | ---: | ---: |
| 每题平均10轮 | 320 | 8,000 / 1,500 | $0.496 | $4.96 |
| 全部跑满50轮 | 1,600 | 20,000 / 2,000 | $4.80 | $48.00 |
| 长历史压力估算 | 1,600 | 100,000 / 4,000 | $19.20 | $192.00 |

费用公式：请求数 × (平均输入 × 输入单价 + 平均输出 × 输出单价) / 1,000,000。输出含计费推理 Token；表格不含额外重试、压缩和缓存写入，不是上界保证。总额度30美元；建议初次接入试跑批准上限1美元，正式实验另审，预留至少5美元应对异常。当前未请求或获得任何新一轮付费批准。
