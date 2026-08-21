# 模块：会话层（SessionController）

## 作用

会话层是整个应用的"单一事实来源"：

- 保存当前会话的**消息列表**（`SessionState.messages`）与**协议无关 transcript**（`ConversationMessage` 列表）
- 管理**权限模式**（default / plan / acceptEdits / bypass）与 Plan Mode 状态机
- 把 transcript + 工具列表组装成模型请求（`build_request()`）
- 统计 Token 用量、触发上下文压缩
- 持久化 / 恢复命名会话

实现位置：`lancher_code/session.py`、`lancher_code/session_store.py`。

## 在系统中的位置

```text
app.py 创建 SessionController
   ↓
TurnRunner（每轮对话读写）  TUI（渲染消息、显示模式/用量）  SettingsService（读取状态）
```

## 核心类

### `SessionController`

| 属性 / 方法 | 作用 |
|---|---|
| `state` | `SessionState`：消息、模式、上下文治理状态 |
| `transcript` | 协议无关消息列表（发给模型的内容） |
| `runtime_mode` | 当前权限模式 |
| `set_runtime_mode(mode)` | 切换模式；进入 plan 时记录 `plan_restore_mode`，退出时设置 `pending_plan_exit_notice` |
| `restore_mode_after_plan()` | `/do` 时恢复到进入 plan 前的模式 |
| `create_user_message(text)` | 创建用户消息并写入 transcript（含动态提醒块） |
| `create_assistant_message()` | 创建 `streaming` 状态的助手消息 |
| `append_message_content(id, delta)` | 流式追加文本 |
| `append_trace_*()` | 向思考轨迹追加 thinking / text / notice / tool_call / tool_result |
| `append_assistant_tool_calls()` / `append_tool_results()` | 把工具调用与结果写入 transcript |
| `complete_message()` / `fail_message()` / `cancel_message()` | 结束一条助手消息 |
| `build_request(tools, ...)` | 组装 `ChatRequest`（system / messages / tools / thinking / mode） |
| `estimate_request_tokens()` | 估算请求 Token |
| `update_context_usage()` | 更新用量锚点 |
| `offload_large_tool_results()` / `compact_context()` | 上下文治理入口 |
| `total_usage()` | 会话累计用量 |
| `save_session(name)` / `auto_save()` / `resume_session(name, force)` / `remove_session` / `rename_session` / `list_saved_sessions` | 会话持久化 |

### `ProjectSessionStore`（`session_store.py`）

- 会话文件：`<项目根>/.lancher/session/<名称>.jsonl`
- 格式：**JSONL**（每行一个 JSON 对象），当前格式版本 `SESSION_FORMAT_VERSION = 3`，支持读取 v1/v2/v3
- 记录类型：`metadata` / `state` / `permissions` / `message` / `transcript`
- 写入采用"临时文件 + `os.replace` 原子替换"，并 `fsync` 落盘
- 名称校验：`^[\w\-\u3400-\u9fff]+$`（中文、字母、数字、下划线、短横线）

## 工作流程：一次请求组装

```text
TurnRunner 调用 build_request(visible_tools)
→ _prompt_context(mode)  ← prompting.build_prompt_context()
→ _request_transcript()  ← 去掉用户消息头部的 <system-reminder>，注入当前动态提醒
→ build_chat_request_payload(...)
→ 返回 ChatRequest（model、system、messages、tools、thinking、mode）
```

## 关键设计

- **transcript 与界面消息分离**：`state.messages`（含 error/cancelled/streaming 状态）只用于界面；`_transcript` 只包含进入模型上下文的完整轮次。失败的助手消息不会写入 transcript（`complete_message` 才追加）。
- **动态提醒注入**：`create_user_message()` 时用 `build_dynamic_context_prompt()` 生成 `<system-reminder>` 块，作为用户消息的第一个 block；发送前由 `_request_transcript()` 移除，并用最新提醒替换（保证 Plan Mode 状态提醒始终新鲜）。
- **用量锚点**：`update_context_usage()` 保存请求快照（`ContextUsageAnchor`），下次估算时若前缀未变则用"锚点 + 增量"快速估算（见 [context-management.md](context-management.md)）。

## 输入与输出

| 方向 | 说明 |
|---|---|
| 输入 | 用户文本、Provider 返回的 `MessageUsage`、工具执行结果、模式切换请求 |
| 输出 | `ChatRequest`（给 Provider）、`TurnEvent` 所需的消息对象（给 TUI）、JSONL 会话文件 |

## 与其他模块的关系

- → `prompting`：所有提示词构建
- → `context_management`：估算、卸载、压缩
- → `session_store`：持久化
- ← `turn_runner`：驱动
- ← `tui_views/chat.py`：渲染与命令

## 注意事项

- `resume_session()` 在会话有未保存改动时必须 `force=True`，否则抛 `SessionStoreError`。
- 会话绑定后（`active_session_name` 非空），任何状态变更触发 `auto_save()`；保存失败只记日志，不阻断对话。
- `context_window` 来自 `provider.context_window` 配置，默认 openai 128000 / claude 200000。
