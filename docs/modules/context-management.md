# 模块：上下文管理（Context Management）

## 作用

解决长会话的两个核心问题：

1. **估算**：请求到底占了多少 token，避免盲目撞上模型上下文窗口
2. **瘦身**：把大工具结果从内存/请求中"卸载"到磁盘，以及把旧轮次压缩成摘要

实现位置：`lancher_code/context_management.py`。该模块大部分是**纯函数**（可独立测试），只有卸载与摘要请求涉及 IO / 网络。

## 关键常量

| 常量 | 值 | 含义 |
|---|---|---|
| `SINGLE_TOOL_RESULT_BYTES` | 50_000 | 单条工具结果超过该字节数即候选卸载 |
| `TOOL_BATCH_BYTES` | 200_000 | 同一批（assistant 轮次）工具结果合计上限 |
| `TOOL_PREVIEW_LINES` / `TOOL_PREVIEW_BYTES` | 20 / 2048 | 卸载后留在上下文里的预览规模 |
| `SUMMARY_OUTPUT_RESERVE` | 20_000 | 压缩时为摘要输出预留的 token |
| `AUTOMATIC_MARGIN` | 13_000 | 自动压缩的额外余量 |
| `EMERGENCY_MARGIN` | 3_000 | 紧急压缩的额外余量 |
| `RECENT_HISTORY_TOKENS` / `RECENT_HISTORY_MESSAGES` | 10_000 / 5 | 压缩后保留的最近历史规模 |
| `RECENT_FILE_LIMIT` / `RECENT_FILE_TOKENS` | 5 / 5_000 | 最近读取文件快照数量与 token 上限 |
| `AUTOMATIC_FAILURE_LIMIT` | 3 | 自动压缩连续失败 3 次启用熔断 |
| `CHARACTERS_PER_TOKEN` | 3.5 | 字符数 → token 的粗略换算 |

## 三个核心能力

### 1. Token 估算（`estimate_request_tokens` / `update_usage_anchor`）

- 把 `ChatRequest` 规范化序列化后按字符数估算（`字符数 / 3.5`）。
- 记录 `ContextUsageAnchor`（上次请求的字符数、消息数与摘要哈希）。
- 下次请求若 **system/tools 形状相同、消息前缀相同、字符数不减少**，则用 `锚点 token + 新增字符数估算`，避免每次全量估算。

### 2. 工具结果卸载（`offload_tool_results`）

```text
遍历 transcript 收集 tool_result 块
→ 找出新结果中超过单条阈值，或让同批合计超过批阈值的调用
→ 把完整文本写入 .lancher/context/<context_id>/tool-results/<sha256(call_id)>.txt
→ 原文替换为预览（大小 / 完整内容路径 / 前 20 行）
→ 已卸载的 call_id 记入 seen_call_ids（同一会话不重复卸载）
```

- 写入使用临时文件 + `os.replace` 原子替换，并校验路径不越过项目根。
- 预览提示模型："需要精确原文时请使用 `read_file` 重新读取"。

### 3. 摘要压缩（`compact_transcript`）

```text
按 user 消息把 transcript 切成"完整轮次组"
→ 估算摘要请求，超限则按策略丢弃最旧组（前 3 次每次丢 1 组，之后按 20% 比例）
→ 用单独请求让模型生成 <summary> 摘要（必须包含九个固定章节，顺序固定）
→ 解析校验摘要（parse_summary）
→ 保留最近历史（5 条消息 / 10k token）
→ 拼装压缩后的 transcript：
   [user: 以下是较早会话的压缩历史]
   [assistant: 摘要]
   [user: 恢复上下文提示（最近读取文件 + 可见工具 + 边界提醒）]
   [最近历史...]
```

摘要请求约束：

- 系统提示要求模型只输出 `<summary>...</summary>`，且必须严格包含九个 `##` 章节（`SUMMARY_HEADINGS`）
- 任何偏差（标签缺失/重复、章节缺失/乱序）都会抛 `ContextCompactionError`
- 摘要请求若返回工具调用同样视为错误

### 辅助能力

- `record_file_snapshot()`：`read_file` 成功后记录最近读取文件快照（最多 5 个，截断到 5k token），供压缩后恢复上下文使用
- `automatic_threshold()`：自动压缩触发阈值
- `build_recovery_prompt()`：压缩后的"恢复上下文"提示

## 触发时机

| 场景 | 触发方 | 说明 |
|---|---|---|
| 自动压缩 | `TurnRunner._run_turn` | 每轮组装请求前估算，超过阈值则压缩 |
| 紧急压缩 | `TurnRunner._run_turn` | 模型返回 `ProviderPromptTooLongError` 时 |
| 手动压缩 | `/compact` 命令 | `TurnRunner.compact_context()`，`persist=True` 写会话文件 |
| 结果卸载 | `TurnRunner` 每轮循环前 | `SessionController.offload_large_tool_results()` |

## 与其他模块的关系

- ← `SessionController`：`compact_context()` / `offload_large_tool_results()` / `record_read_file_result()`
- ← `TurnRunner`：读取阈值常量、触发压缩
- → `ChatProvider`：摘要请求使用同一 `stream_chat()`
- 文件系统：`.lancher/context/<context_id>/tool-results/`（卸载目录，gitignore）

## 注意事项

- 估算值是基于字符数的**近似值**，不是模型真实 token 数。
- 自动压缩有熔断：连续失败 3 次后 `automatic_compaction_disabled=True`，仅当估算超过 `context_window - 3000` 时才会强制重试。
- 压缩会丢弃最旧轮次的细节，恢复依赖"最近读取文件快照 + 摘要"，模型被明确要求不得猜测原文。
