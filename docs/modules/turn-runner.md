# 模块：工具循环（TurnRunner）

## 作用

`TurnRunner` 负责把"一条用户输入"变成"一轮完整的 ReAct 工具循环"：

- 驱动模型多次请求（最多 `tool_loop_limit` 次）
- 拼接流式工具调用、执行工具、把结果送回模型
- 触发自动 / 紧急上下文压缩
- 支持取消、未知工具熔断、非阻塞审批，以及忙时补充与待发送队列
- 以 **异步生成器 + 事件队列** 的方式把进度暴露给 TUI

公开入口在 `lancher_code/agent/runner.py`，模型选择由 `agent/selection.py` 管理，输入队列在 `inputs.py`，流式响应收集在 `streaming.py`，工具批次收尾在 `tool_batch.py`，界面事件类型在 `events.py`。

## 在系统中的位置

```text
tui/app.py  ──run_user_turn()──▶  TurnRunner
                                              │
            ┌───────────────┬────────────────┼────────────────┐
            ▼               ▼                ▼                ▼
      SessionController  ChatProvider   ToolExecutor    context.compaction
```

## 核心类

### `TurnRunner`

| 成员 | 作用 |
|---|---|
| `run_user_turn(text)` | 异步生成器：发布完整轮次上下文后开放后台任务入口，逐个产出队列里的 `TurnEvent` |
| `resolve_permission_request(resolution)` | TUI 回调：把用户决议写入挂起的 Future |
| `cancel_active_turn()` | 取消当前回合（令牌 + 任务 + 挂起权限 Future） |
| `stop_and_wait()` | 界面关闭时取消当前任务并等待工具、子进程和管道收尾 |
| `has_active_turn` | 是否有回合在跑 |
| `compact_context()` | 手动压缩（`/compact`），模型响应中禁止 |
| `set_phase(phase)` / `set_permission_policy(policy)` | 分别切换阶段和策略；运行、审批、压缩期间拒绝修改 |
| `enqueue_input(text, delivery)` | 接收下一轮或当前任务补充，返回 `PendingInput` 回执 |
| `update_pending_input` / `remove_pending_input` / `convert_pending_input` | 编辑、移除、转换未消费消息；消费后不允许重复转换 |
| `pause_queue()` / `resume_queue()` / `run_next_queued_turn()` | 暂停、明确继续，以及原子消费下一项 |
| `prepare_plan_execution(session_id, digest)` | 校验并消费就绪快照，冻结正文，切执行阶段，保留策略 |

### `_ActiveTurn`

内部数据结构：`task_id`、`accepting_input`、`task`（后台任务）、`queue`（事件队列）、`cancellation_token`、`pending_permissions`（请求 id → Future）。补充绑定具体任务，目标结束后暂停保留，不能自动进入其他任务。

## 工作流程（一次完整回合）

```text
run_user_turn(text)
├─ 创建等待启动门闩的后台任务与事件队列
├─ 发布完整 _ActiveTurn，再开放门闩（兼容立即执行任务的调度）
└─ _run_turn:
    1. create_user_message → 事件 user_message_created
    2. create_assistant_message → 事件 assistant_message_started
    3. 循环（loop_count = 1..tool_loop_limit）：
       a. 列出可见工具、卸载大结果、组装请求
       b. 估算 Token：
          · 达到自动压缩阈值 → 自动压缩 → 重新组装请求
       c. streaming.collect_response：消费 Provider 流事件
          · text_delta → 追加内容 + 事件 assistant_text_delta
          · thinking_delta → 思考轨迹 + progress
          · tool_call_delta → ToolCallAssembler.consume
          · message_end → 记录 usage 与单次完整助手响应；提前 EOF 不执行工具
          · ProviderPromptTooLongError → 紧急压缩后重试一次
       d. assembler.finalize_batch() 得到 ToolCall 列表
          · 参数错误或输出上限 → 整批不执行，以真实编号逐个返回简短错误
       e. 累计用量、更新会话
       f. 有工具调用：
          · 整段助手响应（含原始思考/签名/正文/工具）写 transcript、发 tool_call_started 事件
          · ToolExecutor.execute_calls(...) 执行
          · on_invocation_state：已提交状态 → 对话trace与progress；资源等待带真实阻塞快照
          · on_call_started：进入执行阶段；开始与终态清除旧等待说明
          · 收集 discovered_tool_names（MCP 延迟加载）
          · 发 tool_result_received 事件
          · 未知工具熔断检查（连续 N 次 tool_not_found 停止）
          · continue 下一轮
       g. 当前响应或已启动工具组结束：
          · 若有补充，结束当前 assistant 消息段，创建真实 user 消息及新 assistant 段
          · 无补充且无工具调用，complete_message → assistant_message_completed → turn_completed
    4. 异常处理：
       · CancelledError → cancel_message → turn_cancelled
       · LanCherError → fail_message → turn_failed
       · 其他异常 → 记日志 → fail_message → turn_failed
    5. finally：停止本轮进程、清空权限挂起、flush 会话事件
    6. 后台任务完成回调：发送 _QUEUE_END；启动前被取消也会结束消费者
```

## 事件流（TurnEvent）

`TurnEvent`（定义在 `agent/events.py`）是当前轮次的界面事件协议；任务窗口另通过 runner facade 查询并控制所属 Session 的进程，`kind` 包括：

主要包括 `user_message_created`、`assistant_message_started`、`assistant_text_delta`、`tool_call_started`、`tool_result_received`、`usage_updated`、`progress_updated`、`phase_changed`、`policy_changed`、`permission_request_created`、`permission_request_resolved`、`permission_request_closed`、`pending_input_changed`、`steering_applied`、`assistant_message_completed`、`turn_completed`、`turn_cancelled`、`turn_failed`。

`assistant_message_completed` 是段结束，可因补充而出现多次，用量按段结算；`turn_completed` 才表示整个任务成功，只发一次，UI 据此继续队列。

## 关键设计

- **取消语义**：取消令牌与 Session generation 贯穿审批、资源排队和执行；停止本轮会收尾本轮进程，保留明确交给 Session 的后台进程。取消或失败暂停队列；应用退出通过执行 Runtime 清理所有托管资源。
- **取消事实**：trace绑定稳定invocation_id；运行通知中取消但尚未进入远端execute时，按已提交cancelled终态保留未启动。真正进入远端写工具后的interrupted保留结果未知，不把运行标签直接等同远端请求已发出。
- **停止范围**：`stop_session()` 等到本轮及会话后台全部回收；期间拒绝新轮次与文件执行。用户实际提交的进程输入由独立 Runtime 控制任务持有，关闭详情页不撤销输入。
- **权限挂起**：工具需要确认时，`_request_permission()` 创建 Future 并发送 `permission_request_created`；UI 挂载面板后继续消费事件，用户决议按请求 ID 回传。面板通过 closed 事件移除，过期请求无效。
- **补充边界**：当前响应结束或正在运行的并发工具组结束后生效；后续工具补齐 `steering_superseded` 结果而不执行。撤销旧审批使用独立 superseded 决议，不写规则，也不伪装为用户拒绝。
- **计划快照**：只有本任务成功调用 `write_plan_file` 且计划回合成功结束才 ready；新计划开始即失效旧版本。执行用会话内冻结正文，磁盘中的旧计划文件不能成为已确认计划。
- **自动整理阈值**：`ContextBudget` 按模型窗口、实际输出额度和安全余量分配，先治理长工具结果，再按需要摘要；发送前用硬输入额度检查。
- **紧急压缩**：模型报 `ProviderPromptTooLongError` 时，先卸载大结果再摘要，验证成功后在限制次数内重试；候选仍无法装入实际请求预算时明确失败。
- **请求记账**：每个实际模型请求独立记录，摘要、重试和中断都保留已上报消耗；界面消息用量从该请求记录派生，不替代请求事实。
- **未知工具熔断**：连续 `unknown_tool_streak_limit`（默认 3）次 `tool_not_found` 即停止本轮，避免无效循环。

## 输入与输出

| 方向 | 说明 |
|---|---|
| 输入 | 用户文本；TUI 的权限决议、取消请求、阶段与策略切换 |
| 输出 | `TurnEvent` 异步流（TUI 消费）；副作用：会话状态变更、自动保存、MCP 工具发现 |

## 与其他模块的关系

- → `SessionController`：读写消息 / 组装请求 / 压缩
- → `ChatProvider`：`stream_chat()`
- → `ToolExecutor`：执行工具
- → `ToolRegistry`：列出可见工具 / 延迟工具索引
- → `ToolCallAssembler`：拼接工具调用
- → `context.tokens` / `context.budget` / `context.compaction`：usage 校准、动态额度与压缩入口
- ← `tui/app.py`：唯一消费者

## 注意事项

- `run_user_turn()` 每次调用创建一个新的后台任务，事件流结束后任务被 `gather` 回收。
- 手动、自动与紧急压缩通过统一活动包装调用 `SessionController.compact_context()`。每次操作使用稳定活动 ID，内部摘要重试更新同一条记录；结构化 `compaction_updated` 事件携带状态快照，聊天界面据此更新原控件。
- 手动调用可传入预先创建的 `activity_id` 和 `on_activity` 回调，让 worker 排入时就有可见记录。取消、失败和成功均终结该活动；自动失败后继续本轮会记录 `continued`，原有硬预算和重试边界仍生效。详见 [聊天中的压缩记录](../workflows/context-compaction.md)。
- 不要直接调用 `_run_turn` 内部方法；外部只使用公开 API。
