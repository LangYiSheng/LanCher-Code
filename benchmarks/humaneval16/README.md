# LanCher / mini-SWE-agent：HumanEval 16题预注册实验

当前交付是**可运行的离线评测准备框架**。真实模型调用、在线适配器联调和在线得分均未执行；尚不能直接启动收费批量实验。`online-contract.json` 明确记录未完成项，避免把离线检查伪装成已上线的评测系统。

## 已固定的实验

- LanCher 源码：`f984bb72b46d8eff8d82d148782da83a62a3d308`，通过真实 `create_agent()` SDK 接入。
- 基线：实际安装的 `mini-swe-agent==2.4.6`，使用上游 `DefaultAgent`、`LocalEnvironment` 和 `config/mini.yaml` 模板；依赖见 `baseline-requirements.lock`。
- 数据：OpenAI [human-eval](https://github.com/openai/human-eval/tree/b9453e8f1865df0a0057a1383767caa400acfbcf)，MIT 许可附在 `data/LICENSE`。原始压缩包哈希、抽样种子、16题顺序及快照哈希见 `manifest.json`。
- 抽样：对全部164题计算 `SHA256(seed + ':' + task_id)`，取升序前16题，不根据模型结果挑题。修改样本必须创建新实验。
- 题号：123、88、40、28、85、128、96、18、10、79、100、25、120、34、37、69。
- 每个 Agent 每题一次机会；最多50个主循环轮次。工具调用数独立计数。压缩、网络重试、输出续接另外记请求和费用，不假设1600轮等于1600次所有API请求。
- 每题全新工作目录，只包含 `TASK.md` 和 `solution.py`。相同任务提示，保留各自原生系统提示/工具设计，这是比较完整Agent系统，不是消融实验。
- 判定：将最终完整 `solution.py` 与原始 `test`、`check(entry_point)`组合，所有断言通过才算该题成功。Agent说“完成”或SDK返回`completed`不算通过。
- 原始测试在单个`check()`内可能含循环，输出**测试套件1/1或0/1**，不冒充单条断言通过率。测试套件通过后还需正常返回，提前`sys.exit(0)`不算成功。

## 离线复现

在仓库根目录执行（不需要任何API密钥）：

```bash
uv sync --extra dev
python3.12 -m venv /tmp/he16-mini
/tmp/he16-mini/bin/pip install -r benchmarks/humaneval16/baseline-requirements.lock
.venv/bin/python benchmarks/humaneval16/harness.py offline --output /tmp/he16-grader
.venv/bin/python benchmarks/humaneval16/smoke.py \
  --lancher-python .venv/bin/python \
  --mini-python /tmp/he16-mini/bin/python \
  --output /tmp/he16-agents
.venv/bin/pytest -q benchmarks/humaneval16/test_harness.py tests/agent/test_sdk.py
.venv/bin/python benchmarks/humaneval16/harness.py plan --model gpt-6-luna
```

输出目录必须尚不存在，防止覆盖证据。可直接使用随仓库附带且经过哈希验证的16题快照；重新抽样时将human-eval检出到manifest中的提交，再运行`prepare.py /path/to/human-eval`。

离线控制：官方参考答案16份应全通过；故意抛错的空实现16份应全失败。离线Agent模拟：把参考答案作为脚本化模型响应交给**真实Agent工具**写入文件，随后评分，验证两套接入通路；这不包含模型推理。另让两套Agent连续执行50次，验证不会提前截断且不会发生第51轮。

## 证据与统计

`evidence/grader/`：评分器正负控制；`evidence/agents/`：两套Agent脚本化运行和50轮边界。每行均有`evidence_kind`。

| 标记 | 含义 | 能否报告为模型成功率 |
|---|---|---|
| `offline_grader_control` | 参考/空实现验证评分器 | 否 |
| `offline_agent_simulation` | 真实Agent，脚本化模型 | 否 |
| `measured_online` | 真实模型实验，当前无记录 | 是，需16题完整结果 |

`harness.py summarize results.jsonl`按证据类型和Agent分组，拒绝重复任务，报告缺失任务。在线全部16题才生成`success_rate`；缺失不能隐性剔除。`metrics.py`标准化LanCher JSONL结果和mini原生轨迹；`paired_outcomes`统计同题两者皆过、仅LanCher过、仅mini过、皆失败。

LanCher使用`run_usage`，不只使用最后一条回复的`usage`。mini轨迹无法单独证明网络重试费用完整，必须以网关账本补足。Token缺失为`null`，不写成0；部分账本不能计算完整成本。缓存已包含在input、推理已包含在output，避免重复计价。离线成本0表示没发请求，未知Token仍为null。

主指标是最终代码是否通过测试，Agent状态另记。线上即使达到轮次限制也要评分已有文件。所有基础设施失败保留在16题分母中，并可另报有效运行成功率；不静默重试整题。只运行16题、每题一次，统计波动很大，应同时看配对结果和Wilson区间，不据此宣称普遍优越。HumanEval公开多年，存在训练污染风险，函数补全与仓库修复能力也有明显差异。

## 隔离与当前限制

本环境Python3.14.8可运行LanCher；mini使用独立Python3.12.14环境。离线评分统一由主控Python3.14执行；**这还不是最终线上统一运行时**。上线前须在相同镜像/任务Python运行时验证两套Agent。

当前系统有Bubblewrap，但实际执行返回`Creating new namespace failed: Operation not permitted`，没有Docker可执行文件。因此当前离线脚本只执行已知的公开参考答案和固定模拟动作。`trusted_offline=True`是调用方明确声明信任，不是沙箱。不要用它执行真实模型产出的任意代码。

`grade()`默认拒绝无沙箱的候选代码；提供固定digest的Docker评分镜像后，可使用无网络、只读文件系统、非root、资源限制的评分路径。这个Docker路径尚未在本环境实测。Agent必须运行在**另一个**同样约束的容器中，只看到单题工作目录；整个仓库（包含参考答案）不能挂载进去。只读挂载整个主机也不合格。

## 收费实验前的完成条件

1. 完成隔离执行器和验收：两边写权限相同；看不到测试/参考答案/主机凭据/其他题；相同任务Python；记录镜像digest。
2. 实现网关逐请求账本、最大费用预留和全局预算停止；真正API密钥只在网关内存里，Agent工具环境不得获得密钥。禁止原样持久化HTTP头/异常/配置；只存白名单指标。
3. 两边固定相同模型、Responses API、medium推理强度、8192输出上限；LanCher强制`api_mode=responses`，mini使用上游`LitellmResponseModel`。先校验请求参数再做经批准的一题试跑。
4. 用户批准具体模型/范围/预算，才能发付费请求。当前尚未进行密钥有效性或模型权限检查。文档可用不代表账户实际有权限。
5. 一题配对试跑通过且不修改实验口径后再跑32次；试跑费用单列，不重复计入16题正式结果。更改提示/参数后应重新记录实验版本。

## 成本计划（不是实测账单）

报价核查日2026-11-03，Standard短上下文、不计缓存收益：Luna输入$0.10/百万、输出$0.50/百万；Terra输入$1/百万、输出$5/百万。来源：[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)、[Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra)。缓存写入、长上下文、不同服务层费用必须另算。

| 场景：32次任务运行 | 请求假设 | Luna | Terra |
|---|---|---:|---:|
| 规划场景 | 每题10轮，每请求8k输入+1k输出 | $0.416 | $4.16 |
| 重度场景 | 每题50轮，每请求20k输入+2k输出 | $4.80 | $48.00 |
| 大上下文场景 | 每题50轮，每请求128k输入+8192输出 | $27.0336 | $270.336 |

这些只是显式假设下的算术场景，尚无实测依据支持实际平均轮数/Token；也不是成本上限。推荐先Luna，总API预算预留$25，保留$5余量。必须先实现网关预算控制再执行；预算耗尽应标记`budget_stop`，不得偷偷把50轮降成10轮。授权不能替代尚缺的技术条件。
