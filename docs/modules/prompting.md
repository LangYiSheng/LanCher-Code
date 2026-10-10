# 模块：提示词构建（Prompting）

## 作用

提示词构建模块负责把所有"写给模型看"的文本组织起来：

- 系统提示（角色、行为准则、工具使用指南、代码质量规范、安全边界）
- 环境上下文（系统、cwd、日期）
- 工作阶段与独立权限策略，以及计划状态的动态提醒
- MCP 延迟工具索引
- 用户消息的组装

实现位置：`lancher_code/prompting.py`。

## 核心函数

| 函数 | 作用 |
|---|---|
| `build_system_prompt()` | 固定的角色/行为/工具/质量/安全/任务/输出风格提示（多段拼接） |
| `build_environment_prompt(context)` | 当前系统标签、工作目录、日期 |
| `build_prompt_context(...)` | 组装 `PromptContext`（含 `os_label`、`plan_exists` 等） |
| `build_chat_request_payload(...)` | 最终拼装：`system = [系统提示, 环境提示, 动态提醒?, 延迟工具索引?]` + `messages` + `tools` |
| `build_user_message(text, dynamic_context)` | 用户消息 = `<system-reminder>` 块 + 文本块 |
| `build_dynamic_context_prompt(context)` | 聚合所有动态提醒（Plan / MCP / Skill / AGENTS 注入） |
| `build_deferred_tools_prompt(groups)` | `<deferred_tools>` 索引（Server 名 + 工具名，HTML 转义） |

## Plan Mode 动态提醒

`build_plan_mode_prompt()` 根据会话状态返回不同提醒：

| 状态 | 提醒内容 |
|---|---|
| 首次进入 plan（`pending_plan_entry_kind=initial`） | 完整约束：源码只读探索，当前Session workspace可写；最终计划由专用工具确认快照 |
| 重新进入且本会话有计划快照（`reentry`） | 注入该快照，继续修改本会话版本；旧磁盘文件不代表用户批准 |
| 持续多轮（每 5 轮刷新一次） | 重新强调完整约束 |
| 常规 plan 轮次 | 简短的持续生效提醒 |
| 退出 plan 后第一轮（`pending_plan_exit_notice`） | 提示规划已结束，可参考计划文件 |

判断逻辑：`_is_plan_mode_refresh_turn()` —— `(plan_mode_turn_count + 1) % 5 == 1` 且大于 1 时刷新。

讨论阶段追加只读调查提醒；工作阶段始终注入系统约束，审批策略独立显示。工具发现、请求构造及执行器也检查阶段，限制不依赖提示词自律。模型不能自行切到执行；确认执行请求由 UI 与服务层校验计划快照后提交。

## 动态提醒的注入与剥离

```text
SessionController.create_user_message()
  → build_dynamic_context_prompt(...) 生成 <system-reminder> 文本
  → 作为用户消息第一个 block 存入 transcript

SessionController._request_transcript()（发送前）
  → 移除旧 reminder 块
  → 把最新的动态提醒插到最近一条用户消息头部
```

这样保证：**发送给模型的提醒永远是当前状态的最新版本**，而历史 transcript 中保留的是注入时刻的版本（用于展示）。

## 预留的注入点

`build_dynamic_context_prompt()` 目前串联了四个 builder：

- `build_plan_mode_prompt` — 已实现
- `build_mcp_server_prompt` — 当前返回 `None`（占位）
- `build_skill_update_prompt` — 当前返回 `None`（占位）
- `build_agents_injection_prompt` — 当前返回 `None`（占位）

`build_chat_request_payload()` 中也有两处注释标记的预留位置（AGENTS.md 注入、自动记忆注入）。这些是**未实现的设计占位**，新增能力时可在此扩展。

## 输出示例（payload 结构）

```python
PromptPayload(
    system=[
        build_system_prompt(),          # 固定
        build_environment_prompt(...),  # 环境
        "<system-reminder>...</system-reminder>",  # 可选：动态提醒
        "<deferred_tools>...</deferred_tools>",     # 可选：MCP 索引
    ],
    messages=[ConversationMessage(...)],
    tools=[ToolDefinition(...)],
)
```

## 与其他模块的关系

- ← `session.py`：组装请求、注入动态提醒
- ← `context_management.py`：压缩摘要有独立系统提示（`SUMMARY_SYSTEM_PROMPT`，不在本模块）
- → `models.py`：产出 `PromptContext` / `PromptPayload`

## 注意事项

- `build_system_prompt()` 是**固定文本**，不含环境信息；环境信息在 `build_environment_prompt()` 中，二者分离（有对应测试保证）。
- 延迟工具索引中的 Server 标题/描述会经过 HTML 转义（`escape`），防止注入。
- 平台标签：Windows → "Windows PowerShell"，Linux → "Linux shell"，macOS → "macOS shell"（`_runtime_label()`）。


进程工具指南明确区分yield_ms与max_runtime_ms、turn与session归属、Pipe与PTY。收到running句柄不能宣称命令最终成功；后台输出仍通过process_read读取。后台完成摘要作为记录事实附在下一条协议用户消息中，与新任务指令区分，不因后台输出自动开轮。
